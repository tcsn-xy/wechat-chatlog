#!/usr/bin/env python3
"""管理 chatlog「把本机图片回传给 AI 客户端」的显式授权。

普通文本查询不需要授权。只有 MCP 的 ``media`` 工具会把本机缓存的图片缩成
JPEG 预览，作为图片内容块交给客户端；客户端或模型服务商可能因此保存或外发
图片。默认关闭，必须由用户在本机命令行显式执行 ``grant`` 才启用；``revoke``
立即恢复拒绝。

授权文件：``~/.config/chatlog/media-consent.json``（目录 0700、文件 0600）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from chatlog_core.common import MEDIA_CONSENT, TZ, media_consent  # noqa: E402

NOTE = '本机用户明确授权 chatlog 把图片预览回传给 AI 客户端；图片可能被客户端或模型服务商保存、外发。'


class ConsentError(Exception):
    """授权文件操作失败；消息可安全展示。"""


def _check_target():
    """校验授权文件路径：不能是符号链接，父目录不能是符号链接。"""
    target = Path(MEDIA_CONSENT)
    if target.is_symlink():
        raise ConsentError('授权路径是符号链接；拒绝操作')
    parent = target.parent
    if parent.is_symlink():
        raise ConsentError('授权目录是符号链接；拒绝操作')
    return target, parent


def _atomic_write(target, text):
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix='.media-consent-', dir=parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def grant():
    target, _parent = _check_target()
    current = media_consent(target)
    if current is not None:
        print('图片回传已授权；无需修改。')
        print('授权时间：' + str(current.get('granted_at') or '未记录'))
        return 0
    if target.exists():
        # 存在但不是当前用户 0600 的普通授权文件：不覆盖，避免掩盖权限问题。
        raise ConsentError('已存在不可信或未启用的授权文件；请先人工检查或 revoke')
    _atomic_write(target, json.dumps(dict(
        image_external_consent=True,
        granted_at=dt.datetime.now(TZ).isoformat(timespec='seconds'),
        note=NOTE,
    ), ensure_ascii=False, indent=2) + '\n')
    print('已授权：MCP media 工具可把本机图片的 JPEG 预览回传给 AI 客户端。')
    print('注意：图片可能被客户端或模型服务商保存或外发；撤销用 revoke。')
    return 0


def revoke():
    target, _parent = _check_target()
    if not target.exists():
        print('图片回传未授权；无需修改。')
        return 0
    info = target.stat()
    if not target.is_file() or info.st_uid != os.getuid():
        raise ConsentError('授权文件不是当前用户的普通文件；拒绝删除，请人工检查')
    target.unlink()
    print('已撤销：MCP media 不再回传图片；本地 CLI --out 仍可用。')
    return 0


def status(as_json=False):
    consent = media_consent()
    path = Path(MEDIA_CONSENT)
    value = dict(consent=bool(consent), path=str(path),
                 granted_at=(consent or {}).get('granted_at'),
                 exists=path.exists())
    if as_json:
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if consent:
        print('图片回传：已授权（' + str(value['granted_at'] or '未记录时间') + '）')
    elif value['exists']:
        print('图片回传：未授权（授权文件存在但不可信或内容未启用）')
    else:
        print('图片回传：未授权（默认关闭；本地 CLI --out 不受影响）')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='chatlog-media-consent',
        description='管理 chatlog 图片回传给 AI 客户端的显式授权；普通文本查询不需要授权。')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='查看当前授权状态').add_argument('--json', action='store_true')
    sub.add_parser('grant', help='授权 MCP media 回传本机图片预览（默认关闭）')
    sub.add_parser('revoke', help='撤销授权，立即停止回传图片')
    args = parser.parse_args(argv)
    try:
        if args.command == 'grant':
            return grant()
        if args.command == 'revoke':
            return revoke()
        return status(args.json)
    except (ConsentError, OSError) as exc:
        message = str(exc) if isinstance(exc, ConsentError) else '授权文件操作失败，请检查用户目录权限'
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
