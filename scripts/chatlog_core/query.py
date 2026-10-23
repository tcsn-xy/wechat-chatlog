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

MEDIA_COLUMNS = ('i.id,i.platform,i.account_key,i.message_pk,i.msg_kind,i.media_role,'
                 'i.file_name,i.mime,i.bytes,i.unavailable,i.reason,'
                 'i.file_path,i.file_sha256,i.decoded_path,i.decoded_sha256,i.decoded_mime,i.is_thumb,'
                 "(i.data IS NOT NULL OR EXISTS(SELECT 1 FROM blob_parts b "
                 "WHERE b.table_name='media_items' AND b.row_id=i.id)) AS stored_blob")

# 可以尝试生成图片预览的媒体类型；其余类型（音视频、文件）不返回图片内容。
IMAGE_KINDS = ('image', 'sticker', 'emotion', 'thumb')
# 一次查询最多回传多少张预览，避免单条消息的图片把模型上下文挤满。
MAX_PREVIEW_IMAGES = 4


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
            if target.startswith(('wechat:chat:', 'wecom:chat:', 'wechat:message:', 'wecom:message:',
                                  'wechat:media:', 'wecom:media:')):
                self.platforms = [target.split(':')[0]]
        self.key = decode_cursor(args)
        # media 命令把预览字节放在这里，由 CLI/MCP 层取走，不进 JSON 元数据。
        self.preview_images = []
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
        rows = self._select_media(c, message['platform'], message['account_key'], message_pk=message['id'])
        return self.decorate_media(c, rows, message=message)

    def _select_media(self, c, platform, account_key=None, media_id=None, message_pk=None):
        where, params = 'i.platform=%s', [platform]
        if account_key:
            where += ' AND i.account_key=%s'
            params.append(account_key)
        if media_id is not None:
            where += ' AND i.id=%s'
            params.append(media_id)
        if message_pk is not None:
            where += ' AND i.message_pk=%s'
            params.append(message_pk)
        return select(c, 'SELECT ' + MEDIA_COLUMNS + ' FROM media_items i WHERE ' + where + ' ORDER BY i.id', params)

    def decorate_media(self, c, rows, message=None):
        """补齐媒体行的引用、路径状态、caption、语音转写和 viewable 提示。

        路径只从项目受管理的 ``media_store/`` 解析；绝对路径和越界路径一律置空，
        不把数据库内容写到文件系统。``viewable`` 只是“可尝试预览”的提示，
        真正能否解码由 media 命令逐张验证。
        """
        base = (self.root / 'media_store').resolve()
        chat_pks = {}
        decorated = []
        for raw in rows:
            row = dict(raw)
            platform, account = row['platform'], row['account_key']
            message_pk = row.get('message_pk')
            row['message_ref'] = reference(platform, 'message', message_pk) if message_pk else None
            if message is not None:
                row['chat_ref'] = message.get('chat_ref')
            else:
                if message_pk and message_pk not in chat_pks:
                    found = select(c, 'SELECT chat_pk FROM messages WHERE platform=%s AND id=%s', [platform, message_pk])
                    chat_pks[message_pk] = found[0]['chat_pk'] if found else None
                chat_pk = chat_pks.get(message_pk) if message_pk else None
                row['chat_ref'] = reference(platform, 'chat', chat_pk) if chat_pk else None
            row['media_ref'] = reference(platform, 'media', row['id'])
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
                                      [platform, account, sha]) if sha else []
            row['caption_status'] = row['captions'][0]['status'] if row['captions'] else 'not_available'
            row['viewable'] = bool(row['msg_kind'] in IMAGE_KINDS and row['storage'] == 'file'
                                   and (row.get('decoded_path_status') == 'exists_not_verified'
                                        or row.get('file_path_status') == 'exists_not_verified'))
            if row['msg_kind'] == 'voice':
                stub = dict(id=message_pk or 0, platform=platform, account_key=account)
                row['voice_transcripts'] = self.transcripts(c, stub, row['id'])
            decorated.append(row)
        return decorated

    def parse_media_reference(self, value):
        for kind in ('media', 'message'):
            if isinstance(value, str) and value.startswith(('wechat:' + kind + ':', 'wecom:' + kind + ':')):
                platform, pk = self.check_ref(value, kind)
                return platform, kind, pk
        raise QueryError('invalid_reference', '需要明确引用，例如 wechat:media:123 或 wechat:message:123')

    def _select_message(self, c, platform, pk):
        where, params = 'm.platform=%s AND m.id=%s', [platform, pk]
        if self.args.account:
            where += ' AND m.account_key=%s'
            params.append(self.args.account)
        rows = select(c, 'SELECT m.id,m.platform,m.account_key,m.chat_pk,m.chat_id,m.ts FROM messages m WHERE ' + where, params)
        if not rows:
            return None
        row = dict(rows[0])
        row['message_ref'] = reference(platform, 'message', row['id'])
        row['chat_ref'] = reference(platform, 'chat', row['chat_pk']) if row.get('chat_pk') else None
        return row

    def _preview_source(self, row):
        for column in ('decoded_path', 'file_path'):
            value = row.get(column)
            if value and row.get(column + '_status') == 'exists_not_verified':
                return Path(value)
        return None

    def media_view(self, reference, max_edge):
        """按媒体引用或消息引用取图片并逐张生成 JPEG 预览。

        单图失败只记在对应 media 行的 ``preview.error``，不中止整条查询；
        全部失败时把首个失败写入 errors（CLI 退出码 4、MCP isError）。
        预览字节放在 ``self.preview_images``，JSON 结果只带元数据。
        """
        platform, kind, pk = self.parse_media_reference(reference)
        missing = {}

        def fetch(p, c):
            if kind == 'media':
                rows = self._select_media(c, p, self.args.account or None, media_id=pk)
                if not rows:
                    missing.update(code='media_not_found', message='没有找到媒体行；先用 message 查看该消息的 media_ref')
                    return []
                return self.decorate_media(c, rows)
            message = self._select_message(c, p, pk)
            if message is None:
                missing.update(code='message_not_found', message='未找到消息，或消息不属于所选账号')
                return []
            rows = [row for row in self._select_media(c, p, message['account_key'], message_pk=pk)
                    if row['msg_kind'] in IMAGE_KINDS]
            if not rows:
                missing.update(code='no_image_media', message='这条消息没有已入库的图片媒体；可先用 message 查看 media 列表')
                return []
            return self.decorate_media(c, rows, message=message)

        rows = self.attempt(fetch, [platform])
        if not rows and missing:
            raise QueryError(missing['code'], missing['message'])
        if not rows:
            if any(error['platform'] == platform for error in self.errors):
                return [], None
            raise QueryError('media_not_found', '没有找到可查看的媒体')

        failures = []
        for index, row in enumerate(rows):
            if kind == 'message' and index >= MAX_PREVIEW_IMAGES:
                row['preview'] = dict(available=False, skipped=True,
                                      reason=f'一次最多返回 {MAX_PREVIEW_IMAGES} 张图片预览；可用 media_ref 单张查看')
                continue
            source = self._preview_source(row)
            if source is None:
                error = dict(code='image_unavailable',
                             message='本机没有可读取的图片缓存文件；caption 不代表图片内容已补齐')
                row['preview'] = dict(available=False, error=error)
                failures.append(error)
                self.warnings.append(row['media_ref'] + '：' + error['message'])
                continue
            try:
                from .preview import render_preview
                preview = render_preview(source, max_edge=max_edge)
            except QueryError as exc:
                row['preview'] = dict(available=False, error=exc.detail)
                failures.append(exc.detail)
                self.warnings.append(row['media_ref'] + '：' + exc.detail['message'])
                continue
            except Exception:
                error = dict(code='media_preview_failed', message='图片预览生成失败；请重试或换一张')
                row['preview'] = dict(available=False, error=error)
                failures.append(error)
                self.warnings.append(row['media_ref'] + '：' + error['message'])
                continue
            data = preview.pop('data')
            row['preview'] = dict(preview, available=True)
            self.preview_images.append(dict(row_index=index, media_ref=row['media_ref'],
                                            message_ref=row.get('message_ref'),
                                            mime=preview['mime'], data=data))
        if not self.preview_images:
            first = failures[0] if failures else dict(code='image_unavailable', message='没有可返回的图片')
            self.errors.append(dict(first, platform=platform))
        if any(row['preview'].get('skipped') for row in rows):
            skipped = sum(1 for row in rows if row['preview'].get('skipped'))
            self.warnings.append(f'{skipped} 张图片未返回预览（单次上限 {MAX_PREVIEW_IMAGES}）；可用 media_ref 单张查看')
        return rows, None

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
        if self.args.command == 'media':
            return self.media_view(self.args.reference, self.args.max_size)
        if self.args.command in ('message', 'context'):
            return self.message(self.args.reference)
        return self.listing()
