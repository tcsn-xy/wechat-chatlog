"""Read-only attachment index and verified local exports. No client/UI/network calls."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from .common import QueryError, encode_cursor, iso_time, like, page_clause, reference
from .query import MEDIA_COLUMNS, select

CHUNK = 1024 * 1024
MAX_BYTES = 1024 * 1024 * 1024


def hints(row):
    available = (row.get('file_path_status') == 'exists_not_verified'
                 or bool(row.get('stored_blob')))
    return dict(available=available, state='local_available' if available else 'not_cached',
                command=['chatlog', 'download', row['media_ref'], '--json'] if available else None,
                remote_download_supported=False)


def listing(reader):
    """Page media rows (not messages), retaining exact account/chat/message identities."""
    reader.resolve_chat()
    a = reader.args

    def fetch(p, c):
        if reader.chat and reader.chat['platform'] != p:
            return []
        where, params = reader.filters(p)
        where = (where.replace('m.kind=', 'i.msg_kind=')
                 .replace('m.platform=', 'i.platform=').replace('m.account_key=', 'i.account_key=')
                 .replace('m.ts', 'COALESCE(m.ts,i.ts,0)'))
        if a.query:
            where += " AND (i.file_name LIKE %s ESCAPE '!' OR m.text LIKE %s ESCAPE '!')"
            params.extend([like(a.query)] * 2)
        sort = 'COALESCE(m.ts,i.ts,0)'
        extra, values = page_clause(sort, 'i.id', p, reader.key)
        rows = select(c, 'SELECT ' + MEDIA_COLUMNS + ',m.chat_pk,' + sort + ' ts,m.sender_id,m.sender_name '
                      'FROM media_items i LEFT JOIN messages m ON m.id=i.message_pk '
                      'AND m.platform=i.platform AND m.account_key=i.account_key WHERE '
                      + where + extra + ' ORDER BY ' + sort + ' DESC,i.id DESC LIMIT %s',
                      params + values + [a.limit + 1])
        result = reader.decorate_media(c, rows)
        for row in result:
            row['chat_ref'] = reference(p, 'chat', row['chat_pk']) if row.get('chat_pk') else None
            row['time'] = iso_time(row['ts']) if row['ts'] else None
            row['download'] = hints(row)
        return result

    rows = reader.attempt(fetch, [reader.chat['platform']] if reader.chat else None)
    rows.sort(key=lambda r: (r['ts'], r['platform'], r['id']), reverse=True)
    more = len(rows) > a.limit
    rows = rows[:a.limit]
    cursor = (encode_cursor(a, [rows[-1]['ts'], rows[-1]['platform'], rows[-1]['id']])
              if more and not reader.errors else None)
    return rows, dict(next_cursor=cursor, has_more=more if not reader.errors else None,
                      order='descending')


def _chunks(c, row):
    """Read a file or database BLOB in bounded blocks; never fetch a whole large BLOB."""
    if row.get('file_path_status') == 'invalid_path':
        raise QueryError('invalid_media_path', '媒体路径越界，拒绝读取')
    path = row.get('file_path')
    if path and row.get('file_path_status') == 'exists_not_verified':
        with open(path, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise QueryError('media_unavailable', '媒体不是普通文件')
            yield from iter(lambda: stream.read(CHUNK), b'')
        return
    identity = [row['platform'], row['account_key'], row['id']]
    meta = select(c, 'SELECT OCTET_LENGTH(data) n FROM media_items '
                  'WHERE platform=%s AND account_key=%s AND id=%s', identity)
    if not meta:
        raise QueryError('media_unavailable', '媒体已变更，请重新查询')
    size = meta[0]['n']
    if size is not None:
        if size > MAX_BYTES:
            raise QueryError('media_too_large', '附件超过单次导出 1 GiB 上限')
        for offset in range(1, size + 1, CHUNK):
            value = select(c, 'SELECT SUBSTRING(data,%s,%s) value FROM media_items '
                           'WHERE platform=%s AND account_key=%s AND id=%s',
                           [offset, CHUNK] + identity)
            if not value or not value[0]['value']:
                raise QueryError('media_changed', '媒体读取期间发生变更，请重新查询')
            yield bytes(value[0]['value'])
        return
    parts = select(c, "SELECT part_no,OCTET_LENGTH(data) n FROM blob_parts "
                   "WHERE table_name='media_items' AND row_id=%s ORDER BY part_no", [row['id']])
    if not parts:
        raise QueryError('media_not_cached', '本机未缓存该附件；需先在客户端下载，再由媒体同步入库')
    if sum(part['n'] for part in parts) > MAX_BYTES:
        raise QueryError('media_too_large', '附件超过单次导出 1 GiB 上限')
    for expected, part in enumerate(parts):
        if part['part_no'] != expected:
            raise QueryError('media_parts_incomplete', '附件分片缺失，拒绝导出不完整文件')
        for offset in range(1, part['n'] + 1, CHUNK):
            value = select(c, "SELECT SUBSTRING(data,%s,%s) value FROM blob_parts "
                           "WHERE table_name='media_items' AND row_id=%s AND part_no=%s",
                           [offset, CHUNK, row['id'], expected])
            if not value or not value[0]['value']:
                raise QueryError('media_changed', '附件分片读取期间发生变更')
            yield bytes(value[0]['value'])


def _safe_name(value, fallback):
    # Names from chats are untrusted; never interpret them as paths or commands.
    value = re.split(r'[/\\]', value or '')[-1]
    value = ''.join(ch for ch in value if ch.isprintable()).strip(' .')[:120]
    return value or fallback


def export(reader):
    p, kind, pk = reader.parse_media_reference(reader.args.reference)
    if p not in reader.connections:
        raise QueryError('database_unavailable', '附件所属平台不可用')
    c = reader.connections[p]
    # One consistent read snapshot prevents a concurrent media sync changing BLOB parts
    # halfway through export. The connection is read-only and closed by execute().
    c.begin()
    if kind == 'message':
        message = reader._select_message(c, p, pk)
        if message is None:
            raise QueryError('message_not_found', '消息不存在或账号不匹配')
        raw = reader._select_media(c, p, message['account_key'], message_pk=pk)
    else:
        raw = reader._select_media(c, p, reader.args.account, media_id=pk)
    if not raw:
        raise QueryError('media_not_found', '未找到附件；先用 attachments 或 message 查询')
    if len(raw) != 1:
        raise QueryError('ambiguous_media', '消息含多个附件，请指定一个 media_ref',
                         candidates=[reference(p, 'media', r['id']) for r in raw])
    raw = raw[0]
    row = reader.decorate_media(c, [raw])[0]
    # Retain original integrity metadata: decorate_media selects a caption hash separately.
    integrity = select(c, 'SELECT bytes,file_sha256,sha1,content_md5 FROM media_items '
                       'WHERE platform=%s AND account_key=%s AND id=%s',
                       [p, row['account_key'], row['id']])[0]
    from .cli import prepare_output
    if reader.args.out:
        target = prepare_output(reader.args.out, reader.root)
    else:
        # Dedicated private cache only; MCP cannot choose arbitrary output paths.
        folder = reader.root / '.runtime' / 'chatlog-downloads'
        if folder.is_symlink() or not folder.resolve().is_relative_to(reader.root.resolve()):
            raise QueryError('invalid_output_path', '附件导出目录不安全')
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = folder.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise QueryError('invalid_output_path', '附件导出目录必须属于当前用户且权限为 0700')
        name = _safe_name(row.get('file_name'), f"media-{row['id']}.bin")
        target = prepare_output(str(folder / f"{p}-{row['id']}-{name}"), reader.root)
    if reader.args.out and target.exists():
        # No silent replacement of an existing unrelated file, even with --out.
        raise QueryError('output_exists', '目标文件已存在；请选择新的 --out 路径')
    fd, temp = tempfile.mkstemp(prefix='.attachment-', dir=target.parent)
    digests = {k: hashlib.new(k) for k in ('sha256', 'sha1', 'md5')}
    count = 0
    try:
        with os.fdopen(fd, 'wb') as stream:
            for block in _chunks(c, row):
                count += len(block)
                if count > MAX_BYTES:
                    raise QueryError('media_too_large', '附件超过单次导出 1 GiB 上限')
                for digest in digests.values():
                    digest.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        expected_size = integrity.get('bytes')
        if not count or (expected_size is not None and count != expected_size):
            raise QueryError('media_integrity_failed', '附件字节数与索引不一致，未生成下载文件')
        verified = []
        for field, algo in [('file_sha256', 'sha256'), ('sha1', 'sha1'), ('content_md5', 'md5')]:
            expected = integrity.get(field)
            if expected:
                if digests[algo].hexdigest() != expected.lower():
                    raise QueryError('media_integrity_failed', '附件内容校验不通过，未生成下载文件')
                verified.append(algo)
        # link() publishes without overwriting files created concurrently.
        reused = False
        try:
            os.link(temp, target)
        except FileExistsError:
            if reader.args.out or target.is_symlink():
                raise
            info = target.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size != count):
                raise
            digest = hashlib.sha256()
            with target.open('rb') as existing:
                for block in iter(lambda: existing.read(CHUNK), b''):
                    digest.update(block)
            if digest.hexdigest() != digests['sha256'].hexdigest():
                raise
            reused = True
        row['download'] = dict(available=True, state='exported', output_path=str(target),
                               bytes=count, sha256=digests['sha256'].hexdigest(),
                               verified_against_index=verified, remote_download=False, reused=reused)
        row['output_path'] = str(target)
        return [row], None
    except FileExistsError:
        raise QueryError('output_exists', '目标文件已存在；未覆盖') from None
    except OSError:
        raise QueryError('output_failed', '附件读取或导出失败，请检查本机路径权限') from None
    finally:
        os.unlink(temp)
