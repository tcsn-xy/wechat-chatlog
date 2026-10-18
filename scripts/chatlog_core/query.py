from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import time

import pymysql
from pymysql.cursors import DictCursor

from .common import (DATABASES, ROOT, TZ, QueryError, decode_cursor, encode_cursor,
                     iso_time, like, page_clause, parse_ref, reference, safe_error, timestamp)

MESSAGE_COLUMNS = ('m.id,m.platform,m.account_key,m.chat_pk,m.chat_id,m.ts,m.kind,'
                   'm.sender_id,m.sender_name,m.text,m.source_dataset,m.source_db,'
                   'm.source_table,m.local_id,m.server_id,m.has_media,m.has_link')


def connect(config, database):
    c = pymysql.connect(host=config['host'], port=config['port'], user=config['user'],
                        password=config['password'], database=database, charset='utf8mb4',
                        cursorclass=DictCursor, autocommit=True, local_infile=False,
                        connect_timeout=3, read_timeout=12, write_timeout=3)
    try:
        with c.cursor() as cur:
            cur.execute('SET SESSION TRANSACTION READ ONLY')
            cur.execute('SET SESSION MAX_EXECUTION_TIME=8000')
        return c
    except Exception:
        c.close()
        raise


def select(c, sql, params=()):
    with c.cursor() as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())


def decorate(row, kind):
    row = dict(row)
    p = row['platform']
    if 'chat_pk' in row:
        row['chat_ref'] = reference(p, 'chat', row['chat_pk'])
    if kind == 'message':
        row['message_ref'] = reference(p, 'message', row['id'])
        row['time'] = iso_time(row['ts'])
    if kind == 'contact':
        row['contact_ref'] = reference(p, 'contact', row['id'])
    if kind == 'chat':
        row['latest_message_time'] = iso_time(row.get('last_ts'))
    return row


class Reader:
    def __init__(self, config, args, connector=connect, databases=None, root=ROOT):
        self.args = args
        self.root = Path(root)
        self.connections = {}
        self.errors = []
        self.warnings = []
        self.platforms = list(DATABASES) if args.platform == 'all' else [args.platform]
        target = getattr(args, 'reference', None) or args.chat
        if args.platform == 'all' and target:
            if target.startswith(('wechat:chat:', 'wecom:chat:', 'wechat:message:', 'wecom:message:')):
                self.platforms = [target.split(':')[0]]
        self.key = decode_cursor(args)
        self.chat = None
        if args.since and args.until and timestamp(args.since) >= timestamp(args.until):
            raise QueryError('invalid_time_range', '--since 必须早于 --until（结束时间不包含在内）')
        for platform in self.platforms:
            try:
                self.connections[platform] = connector(config, (databases or DATABASES)[platform])
            except Exception as exc:
                self.errors.append(safe_error(exc, platform))

    def close(self):
        connections, self.connections = self.connections, {}
        for c in connections.values():
            try:
                c.close()
            except pymysql.Error:
                pass

    def attempt(self, fn, platforms=None):
        rows = []
        for p in platforms or self.platforms:
            if p not in self.connections:
                continue
            try:
                rows.extend(fn(p, self.connections[p]))
            except Exception as exc:
                self.errors.append(safe_error(exc, p))
        return rows

    def check_ref(self, value, kind):
        p, pk = parse_ref(value, kind)
        if p not in self.platforms:
            raise QueryError('platform_mismatch', '引用的平台与 --platform 不一致')
        return p, pk

    def resolve_chat(self):
        value = self.args.chat
        if not value:
            return
        explicit = value.startswith(('wechat:chat:', 'wecom:chat:'))
        ref = self.check_ref(value, 'chat') if explicit else None

        def find(p, c):
            if ref and ref[0] != p:
                return []
            where = 'platform=%s'
            params = [p]
            if self.args.account:
                where += ' AND account_key=%s'
                params.append(self.args.account)
            if ref:
                where += ' AND chat_pk=%s'
                params.append(ref[1])
            else:
                where += " AND (chat_id=%s OR title LIKE %s ESCAPE '!')"
                params.extend([value, like(value)])
            return [decorate(r, 'chat') for r in select(c, 'SELECT platform,account_key,chat_pk,chat_id,title,last_ts '
                    'FROM chats WHERE ' + where + ' ORDER BY chat_pk LIMIT 201', params)]

        rows = self.attempt(find, [ref[0]] if ref else None)
        relevant_errors = [e for e in self.errors if not ref or e['platform'] == ref[0]]
        if relevant_errors:
            raise QueryError('resolution_incomplete', '会话查找有平台失败；请检查状态或指定可用平台', errors=relevant_errors)
        if not rows:
            raise QueryError('chat_not_found', '没有找到会话；先用 chats 查找明确引用')
        if len(rows) != 1:
            raise QueryError('ambiguous_chat', '匹配多个会话；请用候选中的 chat_ref 重试',
                             candidates=rows[:200], candidates_truncated=len(rows) > 200)
        self.chat = rows[0]

    def filters(self, p, alias='m'):
        where = f'{alias}.platform=%s'
        params = [p]
        if self.args.account:
            where += f' AND {alias}.account_key=%s'
            params.append(self.args.account)
        if self.chat:
            where += f' AND {alias}.chat_pk=%s'
            params.append(self.chat['chat_pk'])
        for value, op in ((self.args.since, '>='), (self.args.until, '<')):
            if value:
                where += f' AND {alias}.ts{op}%s'
                params.append(timestamp(value))
        if self.args.sender:
            where += f' AND {alias}.sender_id=%s'
            params.append(self.args.sender)
        if self.args.kind:
            where += f' AND {alias}.kind=%s'
            params.append(self.args.kind)
        return where, params

    def listing(self):
        command = self.args.command
        self.resolve_chat()
        if command == 'members' and not self.chat:
            raise QueryError('chat_required', 'members 必须指定 --chat')
        kind = {'chats': 'chat', 'contacts': 'contact', 'members': 'member'}.get(command, 'message')

        def fetch(p, c):
            if self.chat and p != self.chat['platform']:
                return []
            if kind == 'message':
                where, params = self.filters(p)
                columns, table, sort, pk = MESSAGE_COLUMNS, 'messages m', 'm.ts', 'm.id'
                if command == 'search':
                    pattern = like(self.args.keyword)
                    if self.args.scope == 'text':
                        where += " AND m.text LIKE %s ESCAPE '!'"
                        params.append(pattern)
                    elif self.args.scope == 'raw':
                        where += " AND EXISTS (SELECT 1 FROM message_raw r WHERE r.message_pk=m.id AND r.raw_content LIKE %s ESCAPE '!')"
                        params.append(pattern)
                    elif self.args.scope == 'voice':
                        where += """ AND EXISTS (SELECT 1 FROM voice_asr_items a JOIN voice_transcripts v ON v.id=a.transcript_id
                          JOIN media_items i ON i.id=a.media_id AND i.platform=a.platform AND i.account_key=a.account_key
                          WHERE a.message_pk=m.id AND a.platform=m.platform AND a.account_key=m.account_key
                          AND i.message_pk=m.id AND i.msg_kind='voice'
                          AND i.file_sha256 <=> a.original_sha256 AND i.decoded_sha256 <=> a.decoded_sha256
                          AND v.status IN ('complete','needs_review') AND v.text LIKE %s ESCAPE '!')"""
                        params.append(pattern)
                    else:
                        where += """ AND EXISTS (SELECT 1 FROM media_items i JOIN image_captions q
                          ON q.platform=i.platform AND q.account_key=i.account_key
                          AND q.image_sha256=COALESCE(i.decoded_sha256,i.file_sha256)
                          WHERE i.message_pk=m.id AND i.platform=m.platform AND i.account_key=m.account_key
                          AND q.status='complete' AND (q.caption LIKE %s ESCAPE '!' OR q.image_text LIKE %s ESCAPE '!'))"""
                        params.extend([pattern, pattern])
            elif kind == 'chat':
                columns = 'c.platform,c.account_key,c.chat_pk,c.chat_id,c.title,c.kind,c.member_count,c.message_count,c.last_ts,c.is_orphan'
                table, sort, pk = 'chats c', 'COALESCE(c.last_ts,0)', 'c.chat_pk'
                where, params = 'c.platform=%s', [p]
                if self.args.query:
                    where += " AND (c.chat_id=%s OR c.title LIKE %s ESCAPE '!')"
                    params.extend([self.args.query, like(self.args.query)])
            elif kind == 'contact':
                columns = 'c.id,c.platform,c.account_key,c.contact_id,c.alias,c.nick_name,c.remark,c.real_name,c.corp_name'
                table, sort, pk = 'contacts c', 'c.id', 'c.id'
                where, params = 'c.platform=%s', [p]
                if self.args.query:
                    where += " AND (c.contact_id=%s OR CONCAT_WS(' ',c.alias,c.nick_name,c.remark,c.real_name) LIKE %s ESCAPE '!')"
                    params.extend([self.args.query, like(self.args.query)])
            else:
                columns = 'c.id,c.platform,h.account_key,c.chat_pk,c.chat_id,c.contact_id,c.room_nick,c.nick_name,c.remark,c.alias,c.shown,c.role,c.message_count,c.in_member_list'
                table, sort, pk = 'chat_members c JOIN chats h ON h.chat_pk=c.chat_pk AND h.platform=c.platform', 'c.id', 'c.id'
                where, params = 'c.platform=%s AND c.chat_pk=%s', [p, self.chat['chat_pk']]
            if kind in ('chat', 'contact') and self.args.account:
                where += ' AND c.account_key=%s'
                params.append(self.args.account)
            extra, values = page_clause(sort, pk, p, self.key)
            params += values + [self.args.limit + 1]
            rows = select(c, f'SELECT {columns},{sort} AS _sort,{pk} AS _pk FROM {table} WHERE '
                          + where + extra + f' ORDER BY {sort} DESC,{pk} DESC LIMIT %s', params)
            decorated = [decorate(row, kind) for row in rows]
            if command == 'search' and self.args.scope == 'voice':
                for row in decorated:
                    row['voice_transcripts'] = self.transcripts(c, row)
            return decorated

        rows = self.attempt(fetch, [self.chat['platform']] if self.chat else None)
        rows.sort(key=lambda r: (r['_sort'], r['platform'], r['_pk']), reverse=True)
        more = len(rows) > self.args.limit
        rows = rows[:self.args.limit]
        cursor = encode_cursor(self.args, [rows[-1]['_sort'], rows[-1]['platform'], rows[-1]['_pk']]) if more else None
        for row in rows:
            row.pop('_sort'); row.pop('_pk')
        if kind == 'message':
            rows.reverse()
        if self.errors:
            self.warnings.append('部分平台未查询成功；本页不完整，恢复后请从第一页重新查询')
            cursor = None
        return rows, dict(next_cursor=cursor, has_more=more if not self.errors else None,
                          order='chronological_within_page' if kind == 'message' else 'descending')

    def message(self, value):
        p, pk = self.check_ref(value, 'message')
        def fetch(p, c):
            where, params = 'm.platform=%s AND m.id=%s', [p, pk]
            if self.args.account:
                where += ' AND m.account_key=%s'; params.append(self.args.account)
            return [decorate(r, 'message') for r in select(c, f'SELECT {MESSAGE_COLUMNS} FROM messages m WHERE ' + where, params)]
        rows = self.attempt(fetch, [p])
        if not rows:
            if any(e['platform'] == p for e in self.errors):
                return [], None
            raise QueryError('message_not_found', '未找到消息，或消息不属于所选账号')
        row = rows[0]
        if self.args.command == 'context':
            return self.context(row)
        try:
            c = self.connections[p]
            row['links'] = select(c, 'SELECT url,title,des,app_type,origin FROM message_links WHERE message_pk=%s AND platform=%s ORDER BY id', [pk, p])
            row['media'] = self.media(c, row)
            if self.args.raw:
                raw = select(c, 'SELECT SUBSTRING(raw_content,%s,%s) raw_content,CHAR_LENGTH(raw_content) total_chars,decoded_from,compress_type,raw_bytes FROM message_raw WHERE message_pk=%s AND platform=%s',
                             [self.args.raw_offset + 1, self.args.raw_limit, pk, p])
                row['raw'] = raw[0] if raw else None
                if row['raw'] is not None:
                    end = self.args.raw_offset + len(row['raw']['raw_content'] or '')
                    row['raw'].update(offset=self.args.raw_offset, next_offset=end if end < (row['raw']['total_chars'] or 0) else None)
        except Exception as exc:
            self.errors.append(safe_error(exc, p))
            self.warnings.append('消息详情未完整读取；缺少的详情字段不代表没有数据')
        return [row], None

    def context(self, anchor):
        p = anchor['platform']
        try:
            c = self.connections[p]
            groups = []
            for op, order, count in (('<', 'DESC', self.args.before), ('>', 'ASC', self.args.after)):
                groups.append(select(c, f'SELECT {MESSAGE_COLUMNS} FROM messages m WHERE m.platform=%s AND m.account_key=%s AND m.chat_pk=%s '
                              f'AND (m.ts {op} %s OR (m.ts=%s AND m.id {op} %s)) ORDER BY m.ts {order},m.id {order} LIMIT %s',
                              [p, anchor['account_key'], anchor['chat_pk'], anchor['ts'], anchor['ts'], anchor['id'], count]))
            rows = [decorate(r, 'message') for r in reversed(groups[0])] + [anchor] + [decorate(r, 'message') for r in groups[1]]
            for r in rows:
                r['is_anchor'] = r['id'] == anchor['id']
            return rows, None
        except Exception as exc:
            self.errors.append(safe_error(exc, p))
            return [], None

    def media(self, c, message):
        rows = select(c, """SELECT i.id,i.msg_kind,i.media_role,i.file_name,i.mime,i.bytes,i.unavailable,i.reason,
          i.file_path,i.file_sha256,i.decoded_path,i.decoded_sha256,i.decoded_mime,i.is_thumb,
          (i.data IS NOT NULL OR EXISTS(SELECT 1 FROM blob_parts b WHERE b.table_name='media_items' AND b.row_id=i.id)) AS stored_blob
          FROM media_items i WHERE i.message_pk=%s AND i.platform=%s AND i.account_key=%s ORDER BY i.id""",
          [message['id'], message['platform'], message['account_key']])
        base = (self.root / 'media_store').resolve()
        for row in rows:
            row['platform'], row['account_key'] = message['platform'], message['account_key']
            row['message_ref'], row['chat_ref'] = message['message_ref'], message['chat_ref']
            row['media_ref'] = reference(message['platform'], 'media', row['id'])
            row['storage'] = 'file' if row['file_path'] else ('database_blob' if row['stored_blob'] else 'missing')
            row['file_status'] = 'missing' if row['storage'] == 'missing' else 'not_verified'
            for column in ('file_path', 'decoded_path'):
                relative = row[column]
                if not relative:
                    continue
                target = (base / relative).resolve()
                if Path(relative).is_absolute() or not target.is_relative_to(base):
                    row[column] = None
                    row[column + '_status'] = 'invalid_path'
                else:
                    row[column] = str(target)
                    row[column + '_status'] = 'exists_not_verified' if target.is_file() else 'missing'
                if column == 'file_path':
                    row['file_status'] = row[column + '_status']
            sha = row.pop('decoded_sha256') or row.pop('file_sha256', None)
            row.pop('file_sha256', None)
            row['captions'] = select(c, 'SELECT prompt_version,status,caption,image_text,error_code,completed_at FROM image_captions '
                                      'WHERE platform=%s AND account_key=%s AND image_sha256=%s ORDER BY id DESC',
                                      [message['platform'], message['account_key'], sha]) if sha else []
            row['caption_status'] = row['captions'][0]['status'] if row['captions'] else 'not_available'
            if row['msg_kind'] == 'voice':
                row['voice_transcripts'] = self.transcripts(c, message, row['id'])
        return rows

    def transcripts(self, c, message, media_id=None):
        params = [message['id'], message['platform'], message['account_key']]
        where = ''
        if media_id is not None:
            where = ' AND a.media_id=%s'
            params.append(media_id)
        return select(c, '''SELECT DISTINCT v.id,v.status,v.text,v.segments,v.language,v.model_version,
          v.input_sha256,v.normalized_sha256,v.duration_ms,v.timing_kind,v.confidence_supported,v.review_reason,v.error_code,v.completed_at
          FROM voice_asr_items a JOIN voice_transcripts v ON v.id=a.transcript_id
          JOIN media_items i ON i.id=a.media_id AND i.platform=a.platform AND i.account_key=a.account_key
          WHERE a.message_pk=%s AND a.platform=%s AND a.account_key=%s AND i.message_pk=a.message_pk AND i.msg_kind='voice'
          AND i.file_sha256 <=> a.original_sha256 AND i.decoded_sha256 <=> a.decoded_sha256''' + where + ' ORDER BY v.id DESC', params)

    def status(self):
        def fetch(p, c):
            params = [p]
            where = 'platform=%s'
            if self.args.account:
                where += ' AND account_key=%s'; params.append(self.args.account)
            accounts = select(c, 'SELECT platform,account_key,display_name FROM accounts WHERE ' + where + ' ORDER BY id', params)
            latest = select(c, 'SELECT ts,imported_at FROM messages WHERE ' + where + ' ORDER BY ts DESC,id DESC LIMIT 1', params)
            realtime = self.read_state(self.root / '.runtime/chat-watch-sync' / (p + '.json'))
            hourly = self.read_state(self.root / ('.runtime/wx-mysql-sync/status.json' if p == 'wechat' else '.runtime/wecom-mysql-sync/status.json'))
            beat = realtime.get('heartbeat_unix')
            fresh = isinstance(beat, (int, float)) and 0 <= time.time() - beat < 20
            active = bool(fresh and realtime.get('enabled') and realtime.get('ready') and realtime.get('mode') in ('direct','inspector'))
            if not active:
                self.warnings.append(p + '：准实时同步未确认健康；状态缺失、过期、停用或尚未就绪')
            return [dict(platform=p, account_key=self.args.account, accounts=accounts, database_connected=True,
                         latest_message_time=iso_time(latest[0]['ts']) if latest else None,
                         sync=dict(scope='platform', realtime_active=active, heartbeat_fresh=fresh,
                                   heartbeat=realtime.get('heartbeat'), mode=realtime.get('mode'),
                                   last_batch_success=realtime.get('last_success'),
                                   last_full_reconciliation=realtime.get('last_full_success'),
                                   hourly_status=hourly.get('status'), hourly_last_success=hourly.get('last_success')),
                         history_coverage='unknown', coverage_note='同步成功和最新消息时间均不能证明全部历史覆盖')]
        return self.attempt(fetch), None

    def read_state(self, path):
        try:
            state = json.loads(path.read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def run(self):
        if self.args.command == 'status':
            return self.status()
        if self.args.command in ('message', 'context'):
            return self.message(self.args.reference)
        return self.listing()
