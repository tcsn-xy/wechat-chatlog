"""Local grants, cursors and delivery identifiers. Not a second task queue."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import fcntl

from .common import QueryError
from .sending import private_directory

STATE = Path.home() / '.local/share/chatlog/bridge'


class Store:
    def __init__(self, root=STATE):
        self.root = private_directory(root).resolve()
        self.path = self.root / 'bridge.sqlite3'
        if self.path.is_symlink():
            raise QueryError('unsafe_bridge_path', '桥接数据库不能是符号链接')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.db() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cursors (platform TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    source_key TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, principal TEXT NOT NULL,
                    platform TEXT NOT NULL, account TEXT NOT NULL, sender TEXT NOT NULL,
                    chat_ref TEXT NOT NULL, message_ref TEXT NOT NULL, thread_id TEXT NOT NULL, chat_id TEXT,
                    workspace TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL,
                    grant_id TEXT,
                    backend TEXT NOT NULL DEFAULT 'codex',
                    detected_at REAL,queued_at REAL,dispatch_seconds REAL,
                    created REAL NOT NULL, status TEXT NOT NULL, native_message_id TEXT,
                    output_hash TEXT, output_at REAL, receipt TEXT);
            ''')
            columns = {r[1] for r in c.execute('PRAGMA table_info(deliveries)')}
            for field in ('grant_id', 'chat_id'):
                if field not in columns:
                    c.execute('ALTER TABLE deliveries ADD COLUMN ' + field + ' TEXT')
            if 'output_text_hash' not in columns:
                c.execute('ALTER TABLE deliveries ADD COLUMN output_text_hash TEXT')
            if 'backend' not in columns:
                c.execute("ALTER TABLE deliveries ADD COLUMN backend TEXT NOT NULL DEFAULT 'codex'")
            for field in ('detected_at','queued_at','dispatch_seconds'):
                if field not in columns:c.execute('ALTER TABLE deliveries ADD COLUMN '+field+' REAL')
            c.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (json.dumps(dict(paused=True, principals=[])),))

    @contextmanager
    def routing_lock(self):
        fd=os.open(self.root/'routing.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a+') as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(f,fcntl.LOCK_UN)

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def config(self):
        with self.db() as c:
            return json.loads(c.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])

    def cursor(self, platform):
        with self.db() as c:
            row = c.execute('SELECT value FROM cursors WHERE platform=?', (platform,)).fetchone()
            return row[0] if row else None

    def advance(self, platform, value):
        with self.db() as c:
            c.execute('INSERT INTO cursors VALUES (?,?) ON CONFLICT(platform) DO UPDATE SET value=MAX(value,excluded.value)', (platform, value))

    def reserve(self, record):
        with self.db() as c:
            keys = list(record)
            return bool(c.execute('INSERT OR IGNORE INTO deliveries (' + ','.join(keys) + ') VALUES (' + ','.join('?' for _ in keys) + ')', tuple(record.values())).rowcount)

    def update(self, token, **fields):
        if set(fields) - {'status', 'native_message_id', 'output_hash', 'output_at', 'receipt','queued_at','dispatch_seconds'}:
            raise ValueError('invalid delivery field')
        with self.db() as c:
            c.execute('UPDATE deliveries SET ' + ','.join(k + '=?' for k in fields) + ' WHERE token=?', (*fields.values(), token))

    def delivery(self, token):
        with self.db() as c:
            row = c.execute('SELECT * FROM deliveries WHERE token=?', (token,)).fetchone()
            if row is None:
                raise QueryError('bridge_reply_not_found', '本次回复凭据不存在')
            return dict(row)

    def recent(self):
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT principal,platform,chat_ref,message_ref,thread_id,backend,created,status,native_message_id,request_id,receipt,dispatch_seconds FROM deliveries ORDER BY created DESC LIMIT 50')]
        for row in rows:
            receipt = json.loads(row.pop('receipt') or '{}')
            data = receipt.get('data')
            row['reply_status'] = data.get('status') if isinstance(data, dict) else None
        return rows

    def is_reply(self, platform, account, chat_ref, digest, since):
        with self.db() as c:
            return c.execute('SELECT 1 FROM deliveries WHERE platform=? AND account=? AND chat_ref=? AND (output_hash=? OR output_text_hash=?) AND output_at>=? LIMIT 1', (platform, account, chat_ref, digest, digest, since)).fetchone() is not None

    def latency(self):
        with self.db() as c:rows=c.execute('SELECT platform,dispatch_seconds FROM deliveries WHERE dispatch_seconds IS NOT NULL ORDER BY created DESC LIMIT 2000').fetchall()
        result={}
        for platform in ('wechat','wecom'):
            values=sorted(r['dispatch_seconds'] for r in rows if r['platform']==platform);n=len(values)
            result[platform]=dict(valid_samples=n,max_seconds=max(values) if n else None,p95_seconds=values[min(n-1,int(n*.95))] if n>=20 else None,validation='measured' if n>=20 else 'insufficient_samples')
        return result

    def admin_token(self):
        path = self.root / 'admin-token'
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise QueryError('unsafe_bridge_token', '管理密钥权限异常')
        else:
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_urlsafe(32))
        return path.read_text().strip()
