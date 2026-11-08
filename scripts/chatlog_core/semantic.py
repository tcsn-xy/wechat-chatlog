"""Optional MiniMax embeddings, private local state and disk-backed vector search.

Only query/chunk text leaves this computer. Database access uses Reader's SELECT
connection; this module never modifies a source/chat database.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from .common import QueryError, parse_ref, reference, timestamp

CONFIG = Path.home() / '.config/chatlog/semantic.json'
STATE = Path.home() / '.local/share/chatlog/semantic'
ENDPOINT = 'https://api.minimaxi.com/v1/embeddings'
MODEL = 'embo-01'
DIMENSIONS = 1536
IDENTITY = MODEL + ':1536:chunks-v1'


def performance(store):
    """Batch sizes change throughput, never embedding/model or source identity."""
    fast = store.get('performance_profile', 'normal') == 'fast'
    return dict(profile='fast' if fast else 'normal', history_page_messages=256 if fast else 32,
                batch_input_bytes=32768 if fast else 3072, batch_max_items=64 if fast else 32,
                page_input_bytes=65536, request_interval_seconds=1, cpu_budget=.10)


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise QueryError('semantic_permissions', '语义索引目录必须属于当前用户且权限为 0700')
    return path


def atomic_json(path, value):
    path = Path(path)
    private_dir(path.parent)
    if path.is_symlink():
        raise QueryError('semantic_permissions', '语义配置或状态不能为符号链接')
    fd, tmp = tempfile.mkstemp(prefix='.semantic-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False); out.flush(); os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_private(path):
    try:
        path = Path(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError()
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise QueryError('semantic_config_required', '缺少安全的语义配置；参见 docs/semantic-search.md') from None


def settings(path=None):
    value = read_private(path or CONFIG)
    if (value.get('endpoint') != ENDPOINT or value.get('model') != MODEL
            or value.get('dimensions') != DIMENSIONS or not isinstance(value.get('api_key'), str)
            or not value['api_key'].strip() or value.get('text_external_consent') is not True
            or value.get('scope') != 'all_history_and_new'):
        raise QueryError('semantic_config_required', '语义配置无效或尚未授权文字向量化')
    return value


def text_of(row):
    """No raw XML, system events or empty media placeholders sent to provider."""
    if row.get('kind') in ('system', 'sys', 'revoke', 'revoked', 'notice'):
        return ''
    value = row.get('text')
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or re.search(r'<\s*(?:\?xml|msg\b|appmsg\b|sysmsg\b)', value, re.I):
        return ''
    if re.fullmatch(r'\[(?:图片|语音|视频|文件|动画表情|表情|未知消息|不支持的消息)\](?:[（(]本机[^\n]*[）)])?', value):
        return ''
    return value


def source_hash(row):
    # Include routing metadata: old sender/chat/account metadata must not be reused.
    return digest(json.dumps([row.get(k) for k in ('platform', 'account_key', 'id', 'chat_pk',
                                                   'sender_id', 'ts', 'kind', 'text')], ensure_ascii=False))


def chunks(text):
    """UTF-8 byte bound is conservative against the legacy 4096-token limit."""
    start = 0
    while start < len(text):
        end, size = start, 0
        while end < len(text) and size + len(text[end].encode('utf-8')) <= 2400:
            size += len(text[end].encode('utf-8')); end += 1
        yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - 80)


class Store:
    """Single-indexer checkpoints and shared request accounting, never chat text."""
    def __init__(self, root=None, create=False):
        self.root = Path(root or STATE)
        path = self.root / 'state.sqlite3'
        if not create and not path.exists():
            raise QueryError('semantic_not_ready', '语义索引尚未启动；运行 chatlog semantic start')
        private_dir(self.root)
        if path.is_symlink():
            raise QueryError('semantic_permissions', '语义状态不能为符号链接')
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise QueryError('semantic_permissions', '语义状态必须属于当前用户且权限为 0600')
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS platforms(platform TEXT PRIMARY KEY,target INTEGER NOT NULL,
            back_cursor INTEGER NOT NULL,live_cursor INTEGER NOT NULL,back_done INTEGER NOT NULL DEFAULT 0,
            reconcile_cursor INTEGER NOT NULL DEFAULT 0,reconcile_target INTEGER NOT NULL DEFAULT 0,
            next_reconcile REAL NOT NULL DEFAULT 0,total INTEGER NOT NULL DEFAULT 0,last_poll REAL);
          CREATE TABLE IF NOT EXISTS sources(platform TEXT NOT NULL,account TEXT NOT NULL,
            message_pk INTEGER NOT NULL,hash TEXT NOT NULL,chunks INTEGER NOT NULL,
            checked_at REAL NOT NULL,PRIMARY KEY(platform,account,message_pk));
          CREATE TABLE IF NOT EXISTS cache(digest TEXT PRIMARY KEY,uid TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS pending(digest TEXT PRIMARY KEY,vector TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,at REAL NOT NULL,
            purpose TEXT NOT NULL,status TEXT NOT NULL,tokens INTEGER NOT NULL DEFAULT 0,
            duration REAL NOT NULL DEFAULT 0);
        ''')
        old = self.get('identity')
        if old and old != IDENTITY:
            self.close()
            raise QueryError('semantic_index_mismatch', '向量模型或分块版本不同；不能混用现有索引')
        self.set('identity', IDENTITY)

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, json.dumps(value)))
        self.db.commit()

    def close(self):
        self.db.close()

    def coverage(self, platforms=None, account=None, chat=None):
        rows = []
        for p in self.db.execute('SELECT * FROM platforms ORDER BY platform'):
            if platforms and p['platform'] not in platforms:
                continue
            counts = self.db.execute('SELECT COUNT(*),SUM(chunks>0),SUM(chunks=0) FROM sources WHERE platform=?',
                                     (p['platform'],)).fetchone()
            historical = self.db.execute('SELECT COUNT(*) FROM sources WHERE platform=? AND message_pk<=?',
                                          (p['platform'], p['target'])).fetchone()[0]
            rows.append(dict(platform=p['platform'], source_rows_at_last_poll=p['total'],
                             checked_messages=counts[0], indexed_messages=counts[1] or 0,
                             skipped_messages=counts[2] or 0, history_scan_complete=bool(p['back_done']),
                             remaining_history_messages_estimate=max(0, self.get('initial_rows_' + p['platform'], p['total']) - historical),
                             remaining_history_id_upper_bound=max(0, p['back_cursor'] - 1) if not p['back_done'] else 0,
                             last_poll=p['last_poll']))
        return dict(scope='platform', requested_account=account, requested_chat=chat,
                    platform_errors=self.get('platform_errors', {}),
                    platforms=rows, history_scan_complete=(len(rows) == len(platforms or ('wechat', 'wecom'))
                                                          and all(x['history_scan_complete'] for x in rows)),
                    note='仅覆盖已入库且已索引的可读文字；历史扫描完成不表示实时无积压或全部原始聊天已入库')


class YieldBackground(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_vectors(value, count):
    vectors = value.get('vectors')
    if not isinstance(vectors, list) or len(vectors) != count:
        raise QueryError('semantic_invalid_vectors', '向量接口返回数量不正确')
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != DIMENSIONS
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)
                or not 0 < sum(x*x for x in vector) < float('inf')):
            raise QueryError('semantic_invalid_vectors', '向量接口返回维度或数值不正确')
    return vectors


class Embedder:
    def __init__(self, config, store, opener=None):
        self.config, self.store = config, store
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def query_waiting(self):
        # A marker only grants priority, never carries a query or credential.
        for path in self.store.root.glob('query-*.request'):
            try:
                pid = int(path.name.split('-')[1]); age = time.time() - path.stat().st_mtime
                os.kill(pid, 0)
                if 0 <= age < 90:
                    return True
            except (ValueError, OSError):
                continue
        return False

    @contextlib.contextmanager
    def priority(self):
        path = self.store.root / f'query-{os.getpid()}-{uuid.uuid4().hex}.request'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def embed(self, texts, purpose):
        if purpose not in ('db', 'query') or not all(isinstance(s, str) and s.strip() for s in texts):
            raise QueryError('semantic_invalid_input', '向量模式或文本无效')
        limits = performance(self.store)
        max_bytes = limits['batch_input_bytes'] if purpose == 'db' else 3072
        max_items = limits['batch_max_items'] if purpose == 'db' else 1
        if not texts or len(texts)>max_items or sum(len(s.encode('utf-8')) for s in texts) > max_bytes:
            raise QueryError('semantic_input_too_long', '单次向量输入过长；请缩短问题或分批索引')
        path = self.store.root / 'requests.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+') as lock:
            deadline = time.monotonic() + 25
            while True:
                if time.monotonic() > deadline:
                    raise QueryError('semantic_busy', '向量服务繁忙；请稍后查询')
                if purpose == 'db' and (self.store.get('paused', False) or self.query_waiting()):
                    raise YieldBackground()
                if purpose == 'db' and getattr(self, 'background_check', None):
                    self.background_check()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise QueryError('semantic_busy', '向量服务繁忙；请稍后查询') from None
                    time.sleep(.05); continue
                delay = max(0, self.store.get('next_request', 0) - time.time())
                if purpose == 'db' and (self.store.get('paused', False) or self.query_waiting()):
                    raise YieldBackground()
                if delay > 0:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    time.sleep(min(delay, .1)); continue
                break
            self.store.set('next_request', time.time() + 1.0)
            if purpose == 'db':
                atomic_json(self.store.root / 'runtime.json', dict(pid=os.getpid(), heartbeat=time.time(), paused=False))
            started = time.monotonic()
            cur = self.store.db.execute('INSERT INTO requests(at,purpose,status) VALUES (?,?,?)',
                                       (time.time(), purpose, 'started'))
            request_pk = cur.lastrowid; self.store.db.commit()
            status, tokens = 'semantic_network_error', 0
            try:
                req = urllib.request.Request(ENDPOINT, data=json.dumps(dict(model=MODEL, texts=texts, type=purpose),
                                             ensure_ascii=False).encode(), headers={
                                             'Authorization': 'Bearer ' + self.config['api_key'],
                                             'Content-Type': 'application/json'})
                try:
                    with self.opener.open(req, timeout=20) as response:
                        wire = response.read(4 * 1024 * 1024 + 1)
                    if len(wire) > 4 * 1024 * 1024:
                        raise QueryError('semantic_invalid_vectors', '向量响应过大')
                    value = json.loads(wire)
                except urllib.error.HTTPError as exc:
                    code = ('semantic_auth' if exc.code in (401, 403) else
                            'semantic_rate_limited' if exc.code == 429 else 'semantic_provider_error')
                    raise QueryError(code, '向量接口拒绝请求；未回退关键词搜索') from None
                except (OSError, ValueError):
                    raise QueryError('semantic_network_error', '向量接口不可用或响应无效；未回退关键词搜索') from None
                if not isinstance(value, dict):
                    raise QueryError('semantic_invalid_vectors', '向量响应结构无效')
                resp = value.get('base_resp')
                code = resp.get('status_code') if isinstance(resp, dict) else None
                if type(code) is not int or code != 0:
                    error = {1002: 'semantic_rate_limited', 1004: 'semantic_auth',
                             1008: 'semantic_quota'}.get(code, 'semantic_provider_error')
                    raise QueryError(error, '向量接口未成功；未回退关键词搜索')
                vectors = validate_vectors(value, len(texts))
                n = value.get('total_tokens', 0)
                tokens = n if type(n) is int and n >= 0 else 0
                status = 'success'
                return vectors
            except QueryError as exc:
                status = exc.detail['code']
                raise
            finally:
                self.store.db.execute('UPDATE requests SET status=?,tokens=?,duration=? WHERE id=?',
                                      (status, tokens, time.monotonic()-started, request_pk))
                self.store.db.commit()


def dependencies():
    try:
        import lancedb
        import pyarrow as pa
        return lancedb, pa
    except ImportError:
        raise QueryError('semantic_dependencies_missing', '请先安装 requirements-semantic.txt') from None


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


class VectorIndex:
    def __init__(self, root, create=False):
        lancedb, pa = dependencies()
        path = Path(root) / 'vectors'
        if not create and not path.exists():
            raise QueryError('semantic_not_ready', '语义索引尚未生成')
        self.db = lancedb.connect(str(path), read_consistency_interval=__import__('datetime').timedelta(0))
        try:
            self.table = self.db.open_table('chunks')
        except (ValueError, FileNotFoundError):
            if not create:
                raise QueryError('semantic_not_ready', '语义索引尚未生成') from None
            schema = pa.schema([('uid', pa.string()), ('digest', pa.string()), ('hash', pa.string()),
                                ('platform', pa.string()), ('account', pa.string()), ('message_pk', pa.uint64()),
                                ('chat_pk', pa.uint64()), ('sender', pa.string()), ('ts', pa.int64()),
                                ('start', pa.int32()), ('end', pa.int32()),
                                ('vector', pa.list_(pa.float32(), DIMENSIONS))])
            self.table = self.db.create_table('chunks', schema=schema)

    def vector(self, uid, content_digest=None):
        rows = self.table.search().where('uid = ' + literal(uid)).select(['vector']).limit(1).to_list()
        if not rows and content_digest:
            rows = self.table.search().where('digest = ' + literal(content_digest)).select(['vector']).limit(1).to_list()
        return rows[0]['vector'] if rows else None

    def vectors(self, mapping):
        """Read cached vectors with one index scan instead of one per message."""
        if not mapping:
            return {}
        rows = self.table.search().where('uid IN (' + ','.join(literal(uid) for uid in set(mapping.values()))
                                        + ')').select(['uid', 'digest', 'vector']).limit(len(mapping)).to_list()
        found = {row['digest']:row['vector'] for row in rows
                 if mapping.get(row['digest']) == row['uid']}
        # Superseded representative chunks can disappear between batches.
        for key in mapping.keys()-found.keys():
            value = self.vector(mapping[key], key)
            if value is not None:
                found[key] = value
        return found

    def upsert(self, rows):
        if rows:
            self.table.merge_insert('uid').when_matched_update_all().when_not_matched_insert_all().execute(rows)

    def candidates(self, vector, filters, limit, exact=False):
        q = self.table.search(vector).distance_type('cosine').where(filters, prefilter=True).limit(limit)
        q = q.bypass_vector_index() if exact else q.nprobes(64).refine_factor(max(2, 8192//limit))
        return q.select(['uid', 'hash', 'platform', 'account', 'message_pk', 'start', 'end', '_distance']).to_list()


def search(reader, config_path=None, root=None):
    args = reader.args
    if not args.query.strip():
        raise QueryError('empty_keyword', '语义查询不能为空')
    if args.chat:
        parse_ref(args.chat, 'chat')  # No nickname or raw-ID resolution.
    reader.resolve_chat()
    config = settings(config_path)
    store = Store(root)
    try:
        platforms = [reader.chat['platform']] if reader.chat else reader.platforms
        coverage = store.coverage(platforms, args.account, args.chat)
        index = VectorIndex(store.root)
        if not coverage['platforms'] or not any(x['indexed_messages'] for x in coverage['platforms']):
            raise QueryError('semantic_not_ready', '所选平台尚无可查询向量；索引正在准备', coverage=coverage)
        where = ['platform IN (' + ','.join(literal(p) for p in platforms) + ')']
        for field, value in (('account', args.account), ('sender', args.sender)):
            if value is not None:
                where.append(field + ' = ' + literal(value))
        if reader.chat:
            where.extend(['chat_pk = ' + str(reader.chat['chat_pk']),
                          'account = ' + literal(reader.chat['account_key'])])
        for value, op in ((args.since, '>='), (args.until, '<')):
            if value:
                where.append('ts ' + op + ' ' + str(timestamp(value)))
        embedder = Embedder(config, store)
        with embedder.priority():
            vector = embedder.embed([args.query], 'query')[0]
            output, seen = [], set()
            candidate_limit = max(100, args.limit * 4)
            stale = 0
            while True:
                candidates = index.candidates(vector, ' AND '.join(where), candidate_limit)
                from .query import MESSAGE_COLUMNS, decorate, select
                current = {}
                for p in platforms:
                    ids = list({r['message_pk'] for r in candidates if r['platform'] == p})
                    if not ids or p not in reader.connections:
                        continue
                    clause, params = reader.filters(p)
                    try:
                        rows = select(reader.connections[p], 'SELECT ' + MESSAGE_COLUMNS + ' FROM messages m WHERE '
                                      + clause + ' AND m.id IN (' + ','.join(['%s']*len(ids)) + ')', params + ids)
                        current.update({(p, r['account_key'], r['id']): r for r in rows})
                    except Exception as exc:
                        from .common import safe_error
                        reader.errors.append(safe_error(exc, p))
                output, seen, stale = [], set(), 0
                for candidate in candidates:
                    key = (candidate['platform'], candidate['account'], candidate['message_pk'])
                    if key in seen:
                        continue
                    row = current.get(key)
                    if not row or source_hash(row) != candidate['hash'] or not text_of(row):
                        stale += 1; continue
                    seen.add(key)
                    row = decorate(row, 'message')
                    snippet = text_of(row)[candidate['start']:candidate['end']]
                    row.update(matched_text=snippet[:600], similarity=round(1-float(candidate['_distance']), 6))
                    output.append(row)
                output.sort(key=lambda r: (-r['similarity'], r['platform'], r['id']))
                if len(output) >= args.limit or len(candidates) < candidate_limit or candidate_limit >= 10000:
                    break
                candidate_limit = min(candidate_limit * 2, 10000)
        if not coverage['history_scan_complete']:
            reader.warnings.append('语义索引尚未覆盖全部历史；空结果不能说明没有相关聊天')
        if stale:
            reader.warnings.append('已剔除失效或内容变化的索引候选；后台核对完成前可能漏召回')
        reader.warnings.append('语义相似度不是事实可信度；请用 message/context 核实原文与上下文')
        return output[:args.limit], dict(order='relevance', has_more=len(output)>args.limit,
                                        next_cursor=None, coverage=coverage,
                                        retrieval='approximate_with_refinement' if store.get('ann_rows', 0) else 'exact_on_indexed_chunks',
                                        candidate_limit_reached=candidate_limit >= 10000)
    finally:
        store.close()
