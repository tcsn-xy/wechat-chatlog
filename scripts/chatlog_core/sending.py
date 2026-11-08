"""Local sending adapters and durable receipts; never writes to chat databases.

The optional native backends remain private local plugins under experiments/.
Receipts contain identities and a request digest, never message bodies or tokens.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import datetime as dt
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

from .common import ROOT, QueryError, parse_ref

CONFIG = Path.home() / '.config/chatlog/sending.json'
STATE = Path.home() / '.local/share/chatlog/sending'
REQUEST_PATTERN = r'[A-Za-z0-9_-]{1,128}'
STATUSES = {'server_accepted', 'submitted_unconfirmed', 'unknown', 'not_sent'}


def validate_request(request_id, text=...):
    if not isinstance(request_id, str) or not re.fullmatch(REQUEST_PATTERN, request_id):
        raise QueryError('invalid_request_id', 'request_id 必须是 1–128 位字母、数字、下划线或连字符')
    if text is not ...:
        try:
            valid = isinstance(text, str) and text.strip() and '\x00' not in text and len(text.encode('utf-8')) <= 16384
        except UnicodeEncodeError:
            valid = False
        if not valid:
            raise QueryError('invalid_text', '正文不能为空、不能含 NUL，且必须为有效 UTF-8、最多 16384 字节')


def load_settings(path=None):
    path = Path(path or CONFIG)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()):
            raise QueryError('sending_config_permissions', '发送配置必须属于当前用户、为普通文件且权限为 0600')
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('platforms'), dict):
            raise ValueError()
        for platform, binding in value['platforms'].items():
            if (platform not in ('wechat', 'wecom') or not isinstance(binding, dict)
                    or not isinstance(binding.get('account_key'), str) or not binding['account_key']
                    or not isinstance(binding.get('self_id'), str) or not binding['self_id']):
                raise ValueError()
        return value['platforms']
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        raise QueryError('invalid_sending_config', '发送配置不可读或无效') from None


def native_module(platform):
    # Fixed local modules only; caller-controlled paths/imports are never accepted.
    names = {'wechat': 'experiments.wechat_sender.sender', 'wecom': 'experiments.wecom_sender.sender'}
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        return importlib.import_module(names[platform])
    except (ImportError, KeyError):
        raise QueryError('sending_unsupported', '该平台尚未安装经过验证的本机发送后端') from None


class NativeAdapter:
    def __init__(self, platform, binding):
        self.platform = platform
        self.binding = binding
        self.module = native_module(platform)
        try:
            self.config = self.module.load_config()
        except self.module.SendError:
            raise QueryError('sender_setup_required', '本机发送后端尚未完成配置') from None
        if not binding.get('self_id') or self.config.get('self_id') != binding['self_id']:
            raise QueryError('sender_account_mismatch', '发送后端账号与 chatlog 绑定不一致；拒绝发送')

    def health(self):
        try:
            value = self.module.doctor(self.config)
        except self.module.SendError as exc:
            return dict(ready=False, status=safe_code(exc.code))
        return dict(ready=value.get('status') == 'ready_for_test',
                    status=safe_code(value.get('status')))

    def send(self, target, text, request_id):
        try:
            return self.module.send_message(target, text, request_id=request_id,
                                            expected_self_id=self.binding['self_id'])
        except self.module.SendError as exc:
            return dict(status='not_sent', error=safe_code(exc.code))

    def media_capabilities(self):
        return verified_media(self.module)

    def send_attachment(self, target, item, request_id):
        # The module must explicitly attest each type after native acceptance.
        if not self.media_capabilities().get(item['type'], {}).get('supported'):
            return dict(status='not_sent', error='sending_type_unsupported', resources_released=True)
        return self.module.send_attachment(target, item, request_id=request_id,
                                           expected_self_id=self.binding['self_id'])


def verified_media(module):
    declared = getattr(module, 'MEDIA_SEND_VERIFIED', {})
    result = {}
    for kind in ('image', 'file'):
        entry = declared.get(kind, {}) if isinstance(declared, dict) else {}
        supported = (isinstance(entry, dict) and entry.get('verified') is True
                     and type(entry.get('max_bytes')) is int and entry['max_bytes'] > 0
                     and callable(getattr(module, 'send_attachment', None)))
        stage=entry.get('validation') if isinstance(entry,dict) else None
        result[kind] = dict(supported=supported, max_bytes=entry['max_bytes'] if supported else None,
                            validation=safe_code(stage) if stage else 'unverified')
    return result


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,100}', value) else 'sender_error'


def capabilities():
    result = {}
    for platform in ('wechat', 'wecom'):
        supported = False
        media = verified_media(None)
        try:
            module = native_module(platform)
            supported = platform == 'wechat' or getattr(module, 'SEND_VERIFIED', False) is True
            media = verified_media(module)
        except QueryError:
            pass
        result[platform] = dict(supported=supported, message_types=(['text'] if supported else []) +
                               [k for k, v in media.items() if v['supported']], requires_setup=True,
                               types=dict(text=dict(supported=supported), **media))
    return result


def status(settings=None, adapter_factory=NativeAdapter):
    result = capabilities()
    try:
        settings = load_settings() if settings is None else settings
        for platform, row in result.items():
            row.update(ready=False, status='sending_unsupported' if not row['supported'] else 'sender_setup_required')
            binding = settings.get(platform)
            if not binding:
                continue
            row['account_key'] = binding.get('account_key')
            try:
                row.update(adapter_factory(platform, binding).health())
            except QueryError as exc:
                row['status'] = exc.detail['code']
            except Exception:
                row['status'] = 'sender_unavailable'
    except QueryError as exc:
        for row in result.values():
            row.update(ready=False, status=exc.detail['code'])
    for row in result.values():
        for value in row['types'].values():
            value.update(ready=bool(value['supported'] and row.get('ready')),
                         status=row.get('status') if value['supported'] else 'sending_type_unsupported')
    return result


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise QueryError('unsafe_receipt_path', '收据目录必须为当前用户所有的真实目录')
    path.chmod(0o700)
    return path


@contextmanager
def receipts(state=None):
    directory = private_directory(state or STATE)
    lock_fd = os.open(directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'receipts.sqlite3'
        if path.is_symlink():
            raise QueryError('unsafe_receipt_path', '收据不能是符号链接')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        path.chmod(0o600)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS receipts '
                       '(request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)')
            yield db


def send_status(request_id, *, state=None, platform='all', account=None):
    validate_request(request_id)
    directory = Path(state or STATE)
    path = directory / 'receipts.sqlite3'
    # Read-only lookup does not provision directories, locks or databases.
    if not path.exists():
        raise QueryError('receipt_not_found', '未找到该请求的发送收据')
    if directory.is_symlink() or path.is_symlink():
        raise QueryError('unsafe_receipt_path', '收据路径不能是符号链接')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise QueryError('unsafe_receipt_path', '收据必须为当前用户所有、权限 0600 的普通文件')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only = ON')
        row = db.execute('SELECT result FROM receipts WHERE request_id=?', (request_id,)).fetchone()
    if not row:
        raise QueryError('receipt_not_found', '未找到该请求的发送收据')
    result = json.loads(row[0])
    check_identity(result, platform, account)
    return result


def check_identity(result, platform, account):
    if platform != 'all' and result['platform'] != platform:
        raise QueryError('platform_mismatch', '引用或收据的平台与 --platform 不一致')
    if account is not None and result['account_key'] != account:
        raise QueryError('sender_account_mismatch', '目标或收据账号与 --account 不一致')


def record_terminal_receipt(request_id, answer, *, platform, account, target_id, state=None):
    """Native service callback only; never called by read-only send_status.

    A late correlated ACK can resolve uncertainty, but must never resume a batch.
    The resident sender supplies the verified process/account/target identity.
    """
    validate_request(request_id)
    if (answer.get('native_finished') is not True or answer.get('status') not in ('server_accepted', 'not_sent')
            or type(answer.get('task_id')) is not int or answer['task_id'] <= 0
            or type(answer.get('ack_ret')) is not int
            or (answer['status'] == 'server_accepted' and answer['ack_ret'] != 0)):
        return False
    if not (Path(state or STATE) / 'receipts.sqlite3').exists():
        return False
    with receipts(state) as db:
        row=db.execute('SELECT result FROM receipts WHERE request_id=?',(request_id,)).fetchone()
        if not row:return False
        result=json.loads(row[0])
        if ((result.get('platform'),result.get('account_key'),result.get('target_id')) != (platform,account,target_id)
                or result.get('status') not in ('unknown','submitted_unconfirmed') or result.get('attempted') is False):
            return False
        result.update({k:answer[k] for k in ('status','task_id','ack_ret')})
        result['resources_released']=answer.get('resources_released') is True
        if isinstance(answer.get('native_message_id'),str) and re.fullmatch(r'[1-9][0-9]{0,19}',answer['native_message_id']):
            result['native_message_id']=answer['native_message_id']
        result.pop('error',None)
        db.execute('UPDATE receipts SET result=? WHERE request_id=?',(json.dumps(result),request_id))
        parent_id=result.get('parent_request_id')
        if parent_id:
            parent=db.execute('SELECT result FROM receipts WHERE request_id=?',(parent_id,)).fetchone()
            if parent:
                batch=json.loads(parent[0])
                for item in batch.get('items',[]):
                    if item['request_id']==request_id:
                        item.update({k:result[k] for k in ('status','task_id','ack_ret','resources_released')})
                        if 'native_message_id' in result:item['native_message_id']=result['native_message_id']
                        item.pop('error',None)
                from .sending_batch import summary
                batch['status'],batch['batch_status']=summary(batch['items'])
                db.execute('UPDATE receipts SET result=? WHERE request_id=?',(json.dumps(batch),parent_id))
        db.commit()
        return True


def send(args, resolve, *, settings=None, state=None, adapter_factory=None, before_item=None, prepared=None):
    adapter_factory = adapter_factory or NativeAdapter
    if getattr(args, 'attachments', None):
        from .sending_batch import send_batch
        return send_batch(args, resolve, settings=settings, state=state, adapter_factory=adapter_factory,
                          before_item=before_item, prepared=prepared)
    platform, _pk = parse_ref(args.chat, 'chat')
    validate_request(args.request_id, args.text)
    if args.platform not in ('all', platform):
        raise QueryError('platform_mismatch', '会话引用与 --platform 不一致')
    fingerprint = hashlib.sha256(json.dumps([args.chat, args.text], ensure_ascii=False).encode()).hexdigest()
    with receipts(state) as db:
        old = db.execute('SELECT fingerprint,result FROM receipts WHERE request_id=?', (args.request_id,)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise QueryError('request_id_content_conflict', '该请求 ID 已用于其他目标或正文；拒绝再次发送')
            result = json.loads(old[1])
            check_identity(result, args.platform, args.account)
            return dict(result, replayed=True)
        chat = resolve()
        check_identity(chat, args.platform, args.account)
        if chat.get('chat_ref') != args.chat or chat.get('platform') != platform:
            raise QueryError('sender_target_mismatch', '会话解析身份不一致；拒绝发送')
        settings = load_settings() if settings is None else settings
        binding = settings.get(platform)
        if not isinstance(binding, dict) or binding.get('account_key') != chat['account_key']:
            raise QueryError('sender_account_mismatch', '该会话账号未绑定发送后端；请先配置发送')
        adapter = adapter_factory(platform, binding)
        health = adapter.health()
        result = dict(platform=platform, account_key=chat['account_key'], chat_ref=args.chat,
                      target_id=chat['chat_id'], request_id=args.request_id,
                      status='not_sent', at=dt.datetime.now(dt.timezone.utc).isoformat())
        if not health.get('ready'):
            result['error'] = safe_code(health.get('status'))
            return result
        if before_item is not None:
            before_item()
        # Persist uncertainty BEFORE dispatch. A crash at any later point may never
        # authorize resubmission, including after a backend/account change.
        result['status'] = 'unknown'
        db.execute('INSERT INTO receipts VALUES (?,?,?)',
                   (args.request_id, fingerprint, json.dumps(result)))
        db.commit()
        try:
            answer = adapter.send(chat['chat_id'], args.text, args.request_id)
            candidate = answer.get('status')
            if candidate in STATUSES:
                result['status'] = candidate
            else:
                result['error'] = 'invalid_sender_response'
            if 'error' in answer:
                result['error'] = safe_code(answer['error'])
            if type(answer.get('task_id')) is int:
                result['task_id'] = answer['task_id']
        except Exception:
            result['error'] = 'sender_result_unknown'
        db.execute('UPDATE receipts SET result=? WHERE request_id=?', (json.dumps(result), args.request_id))
        db.commit()
        return result
