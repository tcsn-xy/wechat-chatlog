#!/usr/bin/env python3
"""Bind an already configured local native sender to one read-only chat account."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from chatlog_core import sending
from chatlog_core.common import DATABASES, QueryError, load_config
from chatlog_core.query import connect, select


def setup(platform, account=None):
    module = sending.native_module(platform)
    try:
        native = module.load_config()
    except module.SendError:
        raise QueryError('sender_setup_required', '请先完成该平台的本机发送后端配置') from None
    identity = native.get('self_id')
    if not identity:
        raise QueryError('sender_account_missing', '发送后端缺少绑定账号')
    connection = connect(load_config(), DATABASES[platform])
    try:
        rows = select(connection, 'SELECT account_key FROM accounts WHERE platform=%s', [platform])
        rows += select(connection, 'SELECT DISTINCT account_key FROM chats WHERE platform=%s', [platform])
    finally:
        connection.close()
    candidates = {r['account_key'] for r in rows if r['account_key'] == identity}
    if account is not None:
        candidates &= {account}
    if len(candidates) != 1:
        raise QueryError('sender_account_mismatch', '发送身份必须与唯一已入库账号精确一致；未写入配置')
    settings = sending.load_settings()
    settings[platform] = dict(account_key=candidates.pop(), self_id=identity)
    directory = sending.private_directory(sending.CONFIG.parent)
    fd, temporary = tempfile.mkstemp(prefix='.sending-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(dict(version=1, platforms=settings), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, sending.CONFIG)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return dict(platform=platform, configured=True, sending=sending.status().get(platform))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=['wechat', 'wecom'])
    parser.add_argument('--account', help='可选，额外限定唯一已入库账号')
    args = parser.parse_args()
    try:
        print(json.dumps(setup(args.platform, args.account), ensure_ascii=False))
    except QueryError as exc:
        print(json.dumps(dict(ok=False, error=exc.detail), ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps(dict(ok=False, error={'code': 'setup_failed', 'message': '发送绑定失败；未输出凭据'})))
        return 4
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
