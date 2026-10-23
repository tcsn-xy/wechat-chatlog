from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path.home() / '.config/chatlog/config.json'
MEDIA_CONSENT = Path.home() / '.config/chatlog/media-consent.json'
DATABASES = {'wechat': 'wx_chat', 'wecom': 'wecom_chat'}
TABLES = ('accounts', 'chats', 'contacts', 'chat_members', 'messages',
          'message_raw', 'message_links', 'media_items', 'image_captions', 'blob_parts',
          'voice_transcripts', 'voice_asr_items', 'voice_asr_batches')
TZ = ZoneInfo('Asia/Shanghai')


class QueryError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.detail = dict(code=code, message=message, **details)


def load_config(path=None):
    path = Path(path or CONFIG)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise QueryError('config_permissions', '配置必须属于当前用户且权限为 0600')
        config = json.loads(path.read_text())
        if (config.get('user') != 'chatlog_reader' or config.get('host') != '127.0.0.1'
                or not isinstance(config.get('password'), str) or not config['password']):
            raise ValueError()
        if not isinstance(config.get('port'), int) or not 1 <= config['port'] <= 65535:
            raise ValueError()
        return config
    except FileNotFoundError:
        raise QueryError('not_installed', '缺少只读配置；请运行项目的 scripts/install_chatlog.py') from None
    except (OSError, ValueError, TypeError, AttributeError):
        raise QueryError('invalid_config', '只读配置不可读或无效；不会回退到管理员账号') from None


def media_consent(path=None):
    """读取「图片可回传给 AI 客户端」的显式授权。

    返回授权内容 dict，或 None（未授权）。文件必须是当前用户所有、权限 0600 的
    普通文件，且 ``image_external_consent`` 为 true；任何缺失或不安全状态都按
    未授权处理，不抛异常、不影响普通文本查询。授权由
    ``scripts/chatlog_media_consent.py grant`` 写入，便于用户撤销和审计。
    """
    path = Path(path or MEDIA_CONSENT)
    try:
        if path.is_symlink():
            return None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            return None
        consent = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(consent, dict) or consent.get('image_external_consent') is not True:
        return None
    return consent


def reference(platform, kind, pk):
    return f'{platform}:{kind}:{pk}'


def parse_ref(value, kind):
    match = re.fullmatch(r'(wechat|wecom):' + kind + r':([1-9][0-9]{0,19})', value or '')
    if not match or int(match[2]) > 18446744073709551615:
        raise QueryError('invalid_reference', f'需要明确引用，例如 wechat:{kind}:123')
    return match[1], int(match[2])


def timestamp(value):
    try:
        date = dt.datetime.fromisoformat(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=TZ)
        return int(date.timestamp())
    except (ValueError, OverflowError):
        raise QueryError('invalid_time', '时间使用 YYYY-MM-DD 或 ISO 8601；无时区时按北京时间') from None


def iso_time(value):
    return dt.datetime.fromtimestamp(int(value), TZ).isoformat() if value is not None else None


def like(value):
    # Explicit escape character avoids dependency on NO_BACKSLASH_ESCAPES.
    return '%' + value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'


def fingerprint(args):
    options = {k: v for k, v in vars(args).items() if k not in ('cursor', 'json', 'limit')}
    return hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()


def encode_cursor(args, key):
    return base64.urlsafe_b64encode(json.dumps([1, fingerprint(args), key]).encode()).decode().rstrip('=')


def decode_cursor(args):
    if not args.cursor:
        return None
    try:
        if len(args.cursor) > 1024:
            raise ValueError()
        version, scope, key = json.loads(base64.urlsafe_b64decode(args.cursor + '=' * (-len(args.cursor) % 4)))
        if version != 1 or scope != fingerprint(args) or len(key) != 3:
            raise ValueError()
        if type(key[0]) is not int or key[1] not in DATABASES or type(key[2]) is not int:
            raise ValueError()
        if not -2**63 <= key[0] < 2**63 or not 0 < key[2] < 2**64:
            raise ValueError()
        return key
    except (ValueError, TypeError, KeyError):
        raise QueryError('invalid_cursor', '分页游标无效，或查询条件已改变；请重新查询') from None


def page_clause(sort, pk, platform, key):
    if key is None:
        return '', []
    value, other, last = key
    if platform == other:
        return f' AND ({sort}<%s OR ({sort}=%s AND {pk}<%s))', [value, value, last]
    return f' AND {sort}' + ('<=%s' if platform < other else '<%s'), [value]


def safe_error(exc, platform):
    if isinstance(exc, QueryError):
        return dict(exc.detail, platform=platform)
    number = exc.args[0] if exc.args else None
    if number in (3024, 1969, 2013):
        return dict(platform=platform, code='query_timeout', message='查询超时；请缩小会话或时间范围')
    if number in (1044, 1045, 1142, 1143):
        return dict(platform=platform, code='permission_denied', message='只读账号认证或权限检查失败')
    if number in (1146, 1054, 1049):
        return dict(platform=platform, code='schema_unavailable', message='需要的数据库、表或字段缺失；查询工具不会自动迁移')
    return dict(platform=platform, code='database_unavailable', message='本机数据库不可用或查询失败')
