"""Resumable, single-writer semantic indexer. All source SQL is SELECT-only."""
from __future__ import annotations

import json
from array import array
import math
import os
import time

from .common import DATABASES, QueryError, load_config
from .query import MESSAGE_COLUMNS, connect, select
from .semantic import (Embedder, Store, VectorIndex, YieldBackground, atomic_json,
                       digest, literal, settings, source_hash, text_of, chunks, performance)

class Indexer:
    def __init__(self, store, config, database_config=None, index=None, embedder=None):
        self.store, self.config = store, config
        self.database_config = database_config or load_config()
        self.connections = {}
        self.index = index or VectorIndex(store.root, create=True)
        self.embedder = embedder or Embedder(config, store)
        self.live_check_at = 0

    def yield_to_live(self):
        if time.monotonic() < self.live_check_at:
            return
        self.live_check_at = time.monotonic() + 3
        for platform in DATABASES:
            state = self.store.db.execute('SELECT live_cursor FROM platforms WHERE platform=?', (platform,)).fetchone()
            if not state:
                continue
            try:
                rows = select(self.connection(platform), 'SELECT id FROM messages WHERE platform=%s AND id>%s LIMIT 1',
                              [platform,state['live_cursor']])
            except Exception:
                continue
            if rows:
                raise YieldBackground()

    def connection(self, platform):
        if platform not in self.connections:
            self.connections[platform] = connect(self.database_config, DATABASES[platform])
        return self.connections[platform]

    def close(self):
        for c in self.connections.values():
            c.close()

    def initialize(self, platform):
        old = self.store.db.execute('SELECT * FROM platforms WHERE platform=?', (platform,)).fetchone()
        if old:
            return
        counts = select(self.connection(platform), 'SELECT COALESCE(MAX(id),0) AS maximum,COUNT(*) AS total '
                        'FROM messages WHERE platform=%s', [platform])[0]
        maximum = int(counts['maximum'])
        self.store.db.execute('INSERT INTO platforms(platform,target,back_cursor,live_cursor,total,last_poll) '
                              'VALUES (?,?,?,?,?,?)', (platform, maximum, maximum+1, maximum, counts['total'], time.time()))
        self.store.db.commit()
        self.store.set('initial_rows_' + platform, counts['total'])

    def rows(self, platform, clause, params, order='ASC'):
        limits=performance(self.store)
        rows=select(self.connection(platform), 'SELECT ' + MESSAGE_COLUMNS + ' FROM messages m WHERE '
                    'm.platform=%s AND ' + clause + ' ORDER BY m.id ' + order + ' LIMIT %s',
                    [platform] + list(params) + [limits['history_page_messages']])
        result,size=[],0
        for row in rows:
            n=len((row.get('text') or '').encode('utf-8'))
            if result and size+n>limits['page_input_bytes']:
                break
            result.append(row);size+=n
        return result

    def process(self, rows):
        """Vector commit precedes checkpoint. A crash replays stable chunk UIDs.

        The small pending-vector cache survives priority yields and restarts,
        avoiding repeated paid calls for an unfinished page/long message.
        """
        started,cpu=time.monotonic(),time.process_time()
        plans, points, needed, vectors = [], [], {}, {}
        for row in rows:
            signature = source_hash(row)
            old = self.store.db.execute('SELECT hash,chunks FROM sources WHERE platform=? AND account=? AND message_pk=?',
                                        (row['platform'], row['account_key'], row['id'])).fetchone()
            if old and old['hash'] == signature:
                continue
            text = text_of(row)
            parts = list(chunks(text)) if text else []
            plans.append((row, signature, parts, old))
            for start, end, value in parts:
                key = digest(value)
                uid = digest(json.dumps([row['platform'], row['account_key'], row['id'], signature, start]))
                points.append(dict(uid=uid, digest=key, hash=signature, platform=row['platform'],
                                   account=row['account_key'], message_pk=row['id'], chat_pk=row['chat_pk'],
                                   sender=row.get('sender_id') or '', ts=row['ts'], start=start, end=end))
                needed.setdefault(key, value)
        prepared=time.monotonic()
        cached_uids={}
        for key in list(needed):
            cached = self.store.db.execute('SELECT vector FROM pending WHERE digest=?', (key,)).fetchone()
            if cached:
                value=cached[0]
                if isinstance(value,bytes):
                    floats=array('f');floats.frombytes(value);vectors[key]=floats.tolist()
                else:
                    vectors[key]=json.loads(value)
                continue
            cached = self.store.db.execute('SELECT uid FROM cache WHERE digest=?', (key,)).fetchone()
            if cached:
                cached_uids[key]=cached['uid']
        vectors.update(self.index.vectors(cached_uids))
        cache_done=time.monotonic()
        unknown = [(key, text) for key, text in needed.items() if key not in vectors]
        new_vectors=len(unknown)
        limits=performance(self.store)
        while unknown:
            batch, size = [], 0
            for item in unknown:
                n = len(item[1].encode('utf-8'))
                if batch and (size + n > limits['batch_input_bytes'] or len(batch)>=limits['batch_max_items']):
                    break
                batch.append(item); size += n
            result = self.embedder.embed([text for _, text in batch], 'db')
            for (key, _), vector in zip(batch, result):
                vectors[key] = vector
                self.store.db.execute('INSERT OR REPLACE INTO pending VALUES (?,?)', (key, array('f',vector).tobytes()))
            self.store.db.commit()
            unknown = unknown[len(batch):]
        embedded=time.monotonic()
        for point in points:
            point['vector'] = vectors[point['digest']]
        self.index.upsert(points)
        committed=time.monotonic()
        # Verify no metadata/source checkpoint advances before durable vector commit.
        with self.store.db:
            for row, signature, parts, old in plans:
                self.store.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?)',
                                      (row['platform'], row['account_key'], row['id'], signature, len(parts), time.time()))
            for point in points:
                self.store.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?)', (point['digest'], point['uid']))
                self.store.db.execute('DELETE FROM pending WHERE digest=?', (point['digest'],))
        # Old vectors cannot produce stale answers: search revalidates the source
        # hash. Remove superseded versions after new rows and state are durable.
        cleanup = []
        for row, signature, parts, old in plans:
            if old:
                cleanup.append('(platform=' + literal(row['platform']) + ' AND account=' + literal(row['account_key'])
                               + ' AND message_pk=' + str(row['id']) + ' AND hash!=' + literal(signature) + ')')
        if cleanup:
            self.index.table.delete(' OR '.join(cleanup))
        finished=time.monotonic()
        self.store.set('last_batch',dict(at=time.time(),messages=len(rows),changed_messages=len(plans),
            unique_texts=len(needed),new_vectors=new_vectors,cache_hits=len(needed)-new_vectors,
            seconds=dict(prepare=round(prepared-started,4),cache=round(cache_done-prepared,4),
                         embedding=round(embedded-cache_done,4),index_commit=round(committed-embedded,4),
                         state_commit=round(finished-committed,4),total=round(finished-started,4),
                         cpu=round(time.process_time()-cpu,4))))
        return len(plans)

    def step_live(self, platform):
        state = self.store.db.execute('SELECT * FROM platforms WHERE platform=?', (platform,)).fetchone()
        rows = self.rows(platform, 'm.id>%s', [state['live_cursor']])
        if not rows:
            self.store.db.execute('UPDATE platforms SET last_poll=? WHERE platform=?', (time.time(), platform))
            self.store.db.commit()
            return False
        self.process(rows)
        self.store.db.execute('UPDATE platforms SET live_cursor=?,last_poll=?,total=total+? WHERE platform=?',
                              (rows[-1]['id'], time.time(), len(rows), platform))
        self.store.db.commit()
        return True

    def step_history(self, platform):
        state = self.store.db.execute('SELECT * FROM platforms WHERE platform=?', (platform,)).fetchone()
        if state['back_done']:
            return self.step_reconcile(platform)
        rows = self.rows(platform, 'm.id<%s AND m.id<=%s', [state['back_cursor'], state['target']], 'DESC')
        self.process(rows)
        if rows:
            self.store.db.execute('UPDATE platforms SET back_cursor=? WHERE platform=?', (rows[-1]['id'], platform))
        if not rows:
            self.store.db.execute('UPDATE platforms SET back_done=1,back_cursor=0,next_reconcile=? WHERE platform=?',
                                  (time.time()+86400, platform))
        self.store.db.commit()
        return bool(rows)

    def step_reconcile(self, platform):
        state = self.store.db.execute('SELECT * FROM platforms WHERE platform=?', (platform,)).fetchone()
        if not state['reconcile_target']:
            if time.time() < state['next_reconcile']:
                return False
            maximum = select(self.connection(platform), 'SELECT COALESCE(MAX(id),0) AS maximum,COUNT(*) AS total '
                             'FROM messages WHERE platform=%s', [platform])[0]
            self.store.db.execute('UPDATE platforms SET reconcile_cursor=0,reconcile_target=?,total=? WHERE platform=?',
                              (int(maximum['maximum']), maximum['total'], platform))
            self.store.db.commit()
            state = self.store.db.execute('SELECT * FROM platforms WHERE platform=?', (platform,)).fetchone()
        rows = self.rows(platform, 'm.id>%s AND m.id<=%s', [state['reconcile_cursor'], state['reconcile_target']])
        self.process(rows)
        if not rows:
            self.store.db.execute('UPDATE platforms SET reconcile_cursor=0,reconcile_target=0,next_reconcile=? WHERE platform=?',
                                  (time.time()+86400, platform))
        else:
            self.store.db.execute('UPDATE platforms SET reconcile_cursor=? WHERE platform=?', (rows[-1]['id'], platform))
        self.store.db.commit()
        return bool(rows)

    def maintain(self):
        count = self.index.table.count_rows()
        indexed = self.store.get('ann_rows', 0)
        if count < 4096 or (indexed and count < indexed * 2 and self.store.get('ann_config') == 'pq192-v1'):
            return
        if self.embedder.query_waiting() or self.store.get('paused', False):
            return
        self.index.table.create_index(metric='cosine', index_type='IVF_PQ',
                                      num_partitions=min(256, max(16, int(math.sqrt(count)))),
                                      num_sub_vectors=192, sample_rate=16, replace=True)
        for column in ('uid', 'digest', 'platform', 'account', 'message_pk', 'chat_pk', 'sender', 'ts'):
            self.index.table.create_scalar_index(column, replace=True)
        self.store.set('ann_rows', count)
        self.store.set('ann_config', 'pq192-v1')
        self.store.set('ann_built_at', time.time())
        # Keep old versions for in-flight readers; normal maintenance is bounded.
        self.index.table.optimize(cleanup_older_than=__import__('datetime').timedelta(days=1))


def run(root=None, config_path=None, stop=None):
    """Resident worker; independent of the bridge and historical chat importer."""
    import fcntl
    os.umask(0o077)
    os.environ.setdefault('RAYON_NUM_THREADS', '2')
    os.environ.setdefault('OMP_NUM_THREADS', '2')
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
    try:
        os.nice(10)
    except OSError:
        pass
    config = settings(config_path)
    store = Store(root, create=True)
    fd = os.open(store.root / 'worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd); store.close()
        raise QueryError('semantic_worker_running', '已有语义索引进程') from None
    indexer = None
    retry, next_maintenance = 1, 0
    try:
        indexer = Indexer(store, config)
        while not stop or not stop.is_set():
            atomic_json(store.root / 'runtime.json', dict(pid=os.getpid(), heartbeat=time.time(), paused=store.get('paused', False)))
            if store.get('paused', False):
                time.sleep(1); continue
            work = False
            try:
                cpu, wall = time.process_time(), time.monotonic()
                active, unavailable = [], {}
                for platform in DATABASES:
                    try:
                        indexer.initialize(platform)
                        active.append(platform)
                    except Exception:
                        unavailable[platform] = 'database_unavailable'
                store.set('platform_errors', unavailable)
                # Drain a bounded live page per platform before any old messages.
                live = False
                for platform in active[:]:
                    try:
                        live = indexer.step_live(platform) or live
                    except Exception as exc:
                        if isinstance(exc, (YieldBackground, QueryError)):
                            raise
                        active.remove(platform)
                        unavailable[platform] = 'database_unavailable'
                        old = indexer.connections.pop(platform, None)
                        if old:
                            try:
                                old.close()
                            except Exception:
                                pass
                work = live
                if not live:
                    indexer.embedder.background_check = indexer.yield_to_live
                    try:
                        for platform in active:
                            work = indexer.step_history(platform) or work
                    finally:
                        indexer.embedder.background_check = None
                if time.time() >= next_maintenance:
                    indexer.maintain(); next_maintenance = time.time()+300
                retry = 1
                store.set('last_error', None)
                store.set('platform_errors', unavailable)
                duty_delay = max(0, (time.process_time()-cpu)/performance(store)['cpu_budget'] - (time.monotonic()-wall))
                if duty_delay:
                    time.sleep(min(duty_delay, 5))
            except YieldBackground:
                time.sleep(.2)
            except Exception as exc:
                code = exc.detail['code'] if isinstance(exc, QueryError) else 'semantic_index_error'
                store.set('last_error', dict(code=code, at=time.time()))
                if code in ('semantic_auth', 'semantic_quota'):
                    store.set('paused', True)
                # Never log provider responses, SQL, text, or exception strings.
                for connection in indexer.connections.values():
                    try:
                        connection.close()
                    except Exception:
                        pass
                indexer.connections.clear()
                time.sleep(retry); retry = min(60, retry*2)
            if not work:
                time.sleep(1)
    finally:
        if indexer:
            indexer.close()
        store.close(); os.close(fd)
