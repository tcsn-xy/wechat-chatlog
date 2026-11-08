"""Ordered outgoing attachments and immutable, owner-only native input files."""
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import warnings

from .common import QueryError

MAX_ATTACHMENTS = 20
# A local allocation guard, not a claim about either client's native limits.
MAX_FILE_BYTES = 100 * 1024 * 1024


class AttachmentAction(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        rows = list(getattr(namespace, 'attachments', None) or [])
        rows.append(dict(type='image' if option_string == '--image' else 'file', path=value))
        namespace.attachments = rows


def add_payload_arguments(parser):
    parser.add_argument('--text', help='文字正文；UTF-8 最多 16384 字节；可与附件一起发送')
    parser.add_argument('--image', dest='attachments', action=AttachmentAction,
                        help='本机 PNG/JPEG 绝对路径；可重复，与 --file 按出现顺序发送')
    parser.add_argument('--file', dest='attachments', action=AttachmentAction,
                        help='本机普通文件绝对路径；附件合计最多 20 个')


def attachment_rows(rows):
    if rows is None:
        return []
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ATTACHMENTS:
        raise QueryError('invalid_attachments', '附件必须为 1–20 项的有序数组')
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'type', 'path'}
                or row['type'] not in ('image', 'file') or not isinstance(row['path'], str)
                or '\x00' in row['path'] or not Path(row['path']).is_absolute()):
            raise QueryError('invalid_attachment', '附件必须指定 image/file 类型和本机普通文件绝对路径')
    return rows


def payload_hash(chat, text, items):
    # Deliberately excludes the source and staging paths. Native-visible names,
    # bytes and ordering, rather than a mutable path, identify a request.
    value = [chat, text, [dict(type=i['type'], name=i['name'], size=i['size'], sha256=i['sha256']) for i in items]]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


@contextmanager
def prepare(rows, root):
    rows = attachment_rows(rows)
    directory = None
    prepared = dict(items=[], retain=False)
    try:
        if rows:
            from .sending import private_directory
            base = private_directory(Path(root) / 'outgoing')
            directory = Path(tempfile.mkdtemp(prefix='batch-', dir=base))
        for index, row in enumerate(rows):
            source = Path(row['path'])
            itemdir = directory / str(index)
            itemdir.mkdir(mode=0o700)
            name = source.name
            if not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise QueryError('invalid_attachment_name', '附件文件名不能包含控制字符')
            destination = itemdir / name
            try:
                # NONBLOCK prevents FIFO/device paths from hanging before fstat.
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as src:
                    before = os.fstat(src.fileno())
                    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_FILE_BYTES:
                        raise QueryError('invalid_attachment_file', '附件须为非空普通文件，且不超过本机 100 MiB 暂存上限')
                    digest = hashlib.sha256()
                    outfd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    size = 0
                    with os.fdopen(outfd, 'wb') as out:
                        while chunk := src.read(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_FILE_BYTES:
                                raise QueryError('attachment_changed', '附件复制期间大小发生变化')
                            digest.update(chunk)
                            out.write(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                    after = os.fstat(src.fileno())
                    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                    if identity(before) != identity(after) or size != before.st_size:
                        raise QueryError('attachment_changed', '附件复制期间发生变化；未提交整批')
                mime = 'application/octet-stream'
                dimensions = {}
                if row['type'] == 'image':
                    from PIL import Image
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter('error', Image.DecompressionBombWarning)
                            with Image.open(destination) as image:
                                if image.format not in ('PNG', 'JPEG'):
                                    raise ValueError()
                                mime = 'image/png' if image.format == 'PNG' else 'image/jpeg'
                                dimensions = dict(width=image.width, height=image.height)
                                image.verify()
                            # JPEG.verify() checks headers but can accept a
                            # truncated entropy stream. Decode before any text
                            # or media item is submitted; preserve original bytes.
                            with Image.open(destination) as image:
                                image.load()
                    except Exception:
                        raise QueryError('invalid_send_image', '图片必须为有效 PNG/JPEG；其它类型请显式使用 --file') from None
                prepared['items'].append(dict(type=row['type'], name=name, size=size,
                    sha256=digest.hexdigest(), mime=mime, path=str(destination), **dimensions))
            except OSError:
                raise QueryError('attachment_unreadable', '附件不可读、为符号链接或不是普通文件') from None
        yield prepared
    finally:
        # An unresolved native call may still own a filename. Never unlink its
        # backing file merely because an HTTP request or CLI timed out.
        if directory is not None and not prepared['retain']:
            shutil.rmtree(directory)
