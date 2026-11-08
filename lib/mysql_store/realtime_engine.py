"""Cooperative per-platform scheduler: fast messages preempt bounded reconciliation."""
from __future__ import annotations
from collections import deque,OrderedDict
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
from . import conn as my,realtime_store as store
from .realtime_sources import row_hash


class Job:
    def __init__(self,desc,iterator,full=False,event_at=None):
        self.desc=desc;self.iterator=iter(iterator);self.full=full;self.event_at=event_at;self.event_cursor=0
        self.rows=0;self.max_id=0;self.pending=None;self.chat_counts={};self.tail=False
    def close(self):
        close=getattr(self.iterator,'close',None)
        if close:close()


class Engine:
    def __init__(self,c,adapter,background_budget=1.0,tail_rows=100,notifications=False):
        self.c=c;self.adapter=adapter;self.descriptors={};self.fast=OrderedDict();self.full=OrderedDict();self.tail=OrderedDict();self.cache={}
        self.background_budget=background_budget;self.tail_rows=tail_rows;self.next_background=0
        self.notifications=notifications
        self.cursors={r['source_key']:r for r in my.query(c,'SELECT * FROM rt_sources')}
        self.results=dict(added=0,updated=0,unchanged=0,stale=0)
        self.latencies=deque(maxlen=2000);self.last_success=None;self.last_full_success=None
        self.full_requested=False;self.full_active=False;self.unresolved=0;self.completed_sources=[];self.started=time.time();self.fast_steps=0
    def schedule(self,paths,event_at=None,force_full=False):
        descriptors,self.unresolved=self.adapter.descriptors(paths,self.c)
        if hasattr(self.adapter,'unresolved_counts'):self.unresolved=sum(self.adapter.unresolved_counts.values())
        descriptors.sort(key=lambda d:d.get('priority_ts',0),reverse=True)
        fresh=[]
        for desc in descriptors:
            key=store.source_key(desc);old=self.cursors.get(key)
            reset=old and (old['schema_hash']!=desc['schema_hash'] or old['file_identity']!=desc['file_identity'] or desc['max_id']<old['cursor_id'])
            if reset:
                with store.writer_lock(self.c):
                    my.execute(self.c,'UPDATE rt_sources SET cursor_id=0,bootstrap=1,scan_active=0,scan_cursor=0,scan_rows=0,scan_counts=NULL WHERE source_key=%s',(key,));self.c.commit()
                old=dict(old,cursor_id=0,bootstrap=1,scan_active=0,scan_cursor=0,scan_rows=0,scan_counts=None);self.cursors[key]=old;self.cache.pop(key,None)
                if key in self.full:self.full.pop(key).close()
                if key in self.tail:self.tail.pop(key).close()
            self.descriptors[key]=desc
            # A new/restored table prioritizes its tail, while the complete history remains a durable obligation.
            cursor=old['cursor_id'] if old else desc['max_id']
            if not old or reset or desc['max_id']>cursor:
                if key not in self.fast or desc['max_id']>self.fast[key].desc['max_id']:
                    if key in self.fast:self.fast.pop(key).close()
                    self.fast[key]=Job(desc,self.adapter.rows(desc,after=cursor),event_at=event_at if old and not reset else None)
                    self.fast[key].event_cursor=cursor;fresh.append(key)
            if not old or reset or old.get('bootstrap') or old.get('scan_active') or force_full:
                self.start_full(key,desc,old)
        # Updated/new conversations must not sit behind the initial historical backlog.
        for key in reversed(fresh):self.fast.move_to_end(key,last=False)
        if force_full:self.full_requested=True;self.full_active=True
    def start_full(self,key,desc,old=None):
        if key in self.full:return
        old=old or self.cursors.get(key,{})
        active=old.get('scan_active');after=old.get('scan_cursor',0) if active and old.get('scan_rows',0)>0 else None
        job=Job(desc,self.adapter.rows(desc,after=after,batch_size=100),full=True)
        if active:
            job.rows=old.get('scan_rows',0);counts=old.get('scan_counts') or '{}'
            job.chat_counts=json.loads(counts) if isinstance(counts,str) else counts
        self.full[key]=job
        if old:
            with store.writer_lock(self.c):
                my.execute(self.c,'UPDATE rt_sources SET scan_active=1 WHERE source_key=%s',(key,));self.c.commit()
            old['scan_active']=1
        self.full_active=True
    def request_tail(self):
        if self.tail:return
        for key,desc in self.descriptors.items():
            if key not in self.tail:
                job=Job(desc,self.adapter.rows(desc,tail=True,batch_size=100,tail_limit=self.tail_rows));job.tail=True;self.tail[key]=job
    def request_full(self):
        self.full_requested=True;self.full_active=True
        for key,desc in self.descriptors.items():
            self.start_full(key,desc)
    def step(self):
        due=time.monotonic()>=self.next_background
        background=self.tail if self.tail else self.full
        if background and due and (not self.fast or self.fast_steps>=4):jobs=background;self.fast_steps=0
        elif self.fast:jobs=self.fast;self.fast_steps+=1
        elif background:return False
        else:jobs=self.fast
        started=time.monotonic()
        try:return self._step(jobs)
        finally:
            if jobs is not self.fast and self.background_budget<1:
                self.next_background=time.monotonic()+(time.monotonic()-started)*(1/self.background_budget-1)
    def _step(self,jobs):
        if not jobs:
            if self.full_active and not self.unresolved:
                self.last_full_success=dt.datetime.now().isoformat();self.full_active=False;self.full_requested=False
            return False
        key=next(iter(jobs));job=jobs[key];desc=job.desc
        if job.pending is None:
            try:job.pending=next(job.iterator)
            except StopIteration:
                cursor=self.cursors.get(key,{}).get('cursor_id',0)
                if not job.full and not job.tail:cursor=max(cursor,desc['max_id'])
                if job.full:self.register_source(desc,job.rows,job.chat_counts)
                result=store.write_batch(self.c,desc,[],cursor,desc['entry']['revision'],complete=job.full,checked_rows=job.rows)
                self.cursors[key]=my.query(self.c,'SELECT * FROM rt_sources WHERE source_key=%s',(key,))[0]
                jobs.pop(key);job.close();return True
        rows=job.pending
        # Full scans may be old; refresh each bounded batch from the latest validated generation.
        current=self.descriptors.get(key,desc)
        if job.full and current['entry']['revision']>desc['entry']['revision']:
            if current['schema_hash']!=desc['schema_hash']:
                jobs.pop(key);job.close();self.full[key]=Job(current,self.adapter.rows(current,batch_size=100),full=True);return True
            newer={r[0]:r for batch in self.adapter.rows(current,ids=[r[0] for r in rows]) for r in batch}
            if len(newer)==len(rows):rows=[newer[r[0]] for r in rows];desc=current
        memo=self.cache.setdefault(key,OrderedDict());records=[];seen=[]
        for row in rows:
            digest=row_hash(row)
            if job.full or memo.get(row[0])!=digest:
                record=self.adapter.decode(desc,row);record['_source_counter']=row[0];records.append(record)
            seen.append((row[0],digest))
        cursor=self.cursors.get(key,{}).get('cursor_id',0)
        if not job.full and not job.tail:cursor=max(cursor,max((r[0] for r in rows),default=cursor))
        counts=dict(job.chat_counts)
        if job.full:
            for record in records:
                cid=record['message']['chat_id'];counts[cid]=counts.get(cid,0)+1
        progress=(max((r[0] for r in rows),default=0),job.rows+len(rows),counts) if job.full else None
        result=store.write_batch(self.c,desc,records,cursor,desc['entry']['revision'],verify_existing=job.full,scan_progress=progress)
        job.chat_counts=counts
        if self.notifications and result['added_ids'] and not job.full and not job.tail and job.event_at is not None:
            from .realtime_bridge import notify
            notify(self.adapter.platform,result['added_ids'],time.time()-(time.monotonic()-job.event_at))
        for counter,digest in seen:
            memo[counter]=digest;memo.move_to_end(counter)
        while len(memo)>2000:memo.popitem(last=False)
        for name in self.results:self.results[name]+=result[name]
        if result['added'] and not job.full and job.event_at is not None:
            samples=sum(1 for counter in result['added_counters'] if counter>job.event_cursor)
            self.latencies.extend([round(time.monotonic()-job.event_at,3)]*samples)
        self.last_success=dt.datetime.now().isoformat();job.rows+=len(rows);job.pending=None
        self.cursors[key]=my.query(self.c,'SELECT * FROM rt_sources WHERE source_key=%s',(key,))[0]
        # One batch per source before rotating: a large group cannot starve new groups.
        jobs.move_to_end(key)
        return True
    def register_source(self,desc,count,chat_counts):
        with store.writer_lock(self.c):
            my.execute(self.c,'DELETE FROM import_sources WHERE platform=%s AND account_key=%s AND dataset=%s AND source_db=%s AND source_table=%s',
                (desc['platform'],desc['account_key'],desc['dataset'],desc['source_db'],desc['source_table']))
            scope='platform=%s AND account_key=%s AND source_dataset=%s AND source_db=%s AND source_table=%s'
            values=(desc['platform'],desc['account_key'],desc['dataset'],desc['source_db'],desc['source_table'])
            if desc['platform']=='wechat':
                pk=my.scalar(self.c,'SELECT chat_pk FROM chats WHERE platform=%s AND account_key=%s AND chat_id=%s',('wechat',desc['account_key'],desc['chat_id']))
                scope='chat_pk=%s AND '+scope;values=(pk,*values)
            stats=my.query(self.c,'SELECT chat_id,COUNT(*) n,MIN(ts) lo,MAX(ts) hi FROM messages WHERE '+scope+' GROUP BY chat_id',values)
            for stat in stats:
                my.execute(self.c,'INSERT INTO import_sources(platform,account_key,dataset,source_db,source_table,chat_id,source_rows,inserted_rows,min_ts,max_ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (desc['platform'],desc['account_key'],desc['dataset'],desc['source_db'],desc['source_table'],stat['chat_id'],chat_counts.get(stat['chat_id'],0),stat['n'],stat['lo'],stat['hi']))
            self.c.commit()
    def status(self):
        values=sorted(self.latencies);n=len(values)
        return dict(**self.results,fast_sources=len(self.fast),full_sources=len(self.full),tail_sources=len(self.tail),background_budget=self.background_budget,unresolved_sources=self.unresolved,
            last_success=self.last_success,last_full_success=self.last_full_success,latency_samples=n,
            latency_p95_seconds=values[min(n-1,int(n*.95))] if n>=20 else None,
            latency_validation='measured' if n>=20 else 'insufficient_samples',
            last_latency_seconds=self.latencies[-1] if n else None)
    def collect(self):
        retained=[job.desc['entry']['folder'] for job in list(self.fast.values())+list(self.full.values())+list(self.tail.values())]
        self.adapter.snapshots.collect(retained)
    def close(self):
        for job in list(self.fast.values())+list(self.full.values())+list(self.tail.values()):job.close()
