"""Durable ordered sends. Receipt replay never resumes a partially sent batch."""
import datetime as dt
import hashlib
import json
import sqlite3
import re

from .common import QueryError, parse_ref
from .send_payload import payload_hash, prepare


def summary(items):
    states = [i['status'] for i in items]
    if all(s == 'server_accepted' for s in states):
        return 'server_accepted', 'complete'
    if 'unknown' in states:
        return 'unknown', 'unknown'
    if 'submitted_unconfirmed' in states:
        return 'submitted_unconfirmed', 'pending'
    if 'server_accepted' in states:
        return 'partial', 'partial'
    return 'not_sent', 'not_sent'


def send_batch(args, resolve, *, settings, state, adapter_factory, before_item=None, prepared=None):
    from . import sending
    platform, _ = parse_ref(args.chat, 'chat')
    sending.validate_request(args.request_id)
    if args.text is not None:
        sending.validate_request(args.request_id, args.text)
    if args.platform not in ('all', platform):
        raise QueryError('platform_mismatch', '会话引用与 --platform 不一致')
    if prepared is None:
        with prepare(args.attachments, state or sending.STATE) as payload:
            return send_batch(args, resolve, settings=settings, state=state,
                              adapter_factory=adapter_factory, before_item=before_item, prepared=payload)
    chat = resolve()
    sending.check_identity(chat, args.platform, args.account)
    if chat.get('chat_ref') != args.chat or chat.get('platform') != platform:
        raise QueryError('sender_target_mismatch', '会话解析身份不一致；拒绝发送')
    fingerprint = payload_hash([args.chat, chat['account_key'], chat['chat_id']], args.text, prepared['items'])
    with sending.receipts(state) as db:
        old = db.execute('SELECT fingerprint,result FROM receipts WHERE request_id=?', (args.request_id,)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise QueryError('request_id_content_conflict', '该请求 ID 已用于其他目标、文字或附件；拒绝再次发送')
            result = json.loads(old[1])
            sending.check_identity(result, args.platform, args.account)
            return dict(result, replayed=True)
        bindings = sending.load_settings() if settings is None else settings
        binding = bindings.get(platform)
        if not isinstance(binding, dict) or binding.get('account_key') != chat['account_key']:
            raise QueryError('sender_account_mismatch', '该会话账号未绑定发送后端')
        adapter = adapter_factory(platform, binding)
        supported = adapter.media_capabilities()
        for item in prepared['items']:
            info = supported.get(item['type'], {})
            if not info.get('supported') or type(info.get('max_bytes')) is not int or info['max_bytes'] <= 0:
                raise QueryError('sending_type_unsupported', '该平台的附件类型尚未完成原生发送验证；整批未发送',
                                 platform=platform, message_type=item['type'])
            if item['size'] > info['max_bytes']:
                raise QueryError('attachment_too_large', '附件超过该平台已验证的发送大小上限；整批未发送')
        health = adapter.health()
        if not health.get('ready'):
            raise QueryError('sender_not_ready', '发送后端未就绪；整批未发送', reason=sending.safe_code(health.get('status')))
        inputs = ([dict(type='text', text=args.text)] if args.text is not None else []) + prepared['items']
        result = dict(platform=platform, account_key=chat['account_key'], chat_ref=args.chat,
                      target_id=chat['chat_id'], request_id=args.request_id, status='not_sent',
                      batch_status='not_sent', at=dt.datetime.now(dt.timezone.utc).isoformat(), items=[])
        for index, item in enumerate(inputs):
            child = 'batch_' + hashlib.sha256((args.request_id + ':' + str(index)).encode()).hexdigest()
            result['items'].append(dict(index=index, type=item['type'], request_id=child,
                                        status='not_sent', attempted=False))

        def persist():
            result['status'], result['batch_status'] = summary(result['items'])
            db.execute('UPDATE receipts SET result=? WHERE request_id=?', (json.dumps(result), args.request_id))
            for item in result['items']:
                record = {k: result[k] for k in ('platform', 'account_key', 'chat_ref', 'target_id', 'at')}
                record.update(item)
                record['parent_request_id'] = args.request_id
                db.execute('UPDATE receipts SET result=? WHERE request_id=?', (json.dumps(record), item['request_id']))
            db.commit()

        # Reserve every identity in the same transaction; collisions with earlier
        # ordinary or batch requests fail before any external action.
        try:
            db.execute('INSERT INTO receipts VALUES (?,?,?)', (args.request_id, fingerprint, json.dumps(result)))
            for row in result['items']:
                db.execute('INSERT INTO receipts VALUES (?,?,?)', (row['request_id'], fingerprint, '{}'))
            persist()
        except sqlite3.IntegrityError:
            db.rollback()
            raise QueryError('request_id_content_conflict', '批量子请求 ID 与已有请求冲突；整批未发送') from None
        for row, item in zip(result['items'], inputs):
            try:
                if before_item is not None:
                    before_item()
            except QueryError as exc:
                row['error'] = sending.safe_code(exc.detail['code'])
                persist()
                break
            # Both batch and child receipts describe uncertainty before dispatch.
            row.update(status='unknown', attempted=True)
            persist()
            if item['type'] != 'text':
                prepared['retain'] = True
            try:
                if item['type'] == 'text':
                    answer = adapter.send(chat['chat_id'], item['text'], row['request_id'])
                else:
                    answer = adapter.send_attachment(chat['chat_id'], item, row['request_id'])
                status = answer.get('status')
                if status in sending.STATUSES:
                    row['status'] = status
                else:
                    row['error'] = 'invalid_sender_response'
                if 'error' in answer:
                    row['error'] = sending.safe_code(answer['error'])
                if type(answer.get('task_id')) is int:
                    row['task_id'] = answer['task_id']
                if isinstance(answer.get('native_message_id'),str) and re.fullmatch(r'[1-9][0-9]{0,19}',answer['native_message_id']):
                    row['native_message_id']=answer['native_message_id']
                row['resources_released'] = answer.get('resources_released') is True
            except Exception:
                row['error'] = 'sender_result_unknown'
            persist()
            if row['status'] != 'server_accepted':
                break
        attempted_media = [i for i in result['items'] if i['type'] != 'text' and i['attempted']]
        prepared['retain'] = any(not i.get('resources_released') for i in attempted_media)
        return result
