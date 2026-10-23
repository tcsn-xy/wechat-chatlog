#!/usr/bin/env python3
"""One-time local provisioning. Never imported by the read-only CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from mysql_store import conn as admin
from chatlog_core.common import CONFIG, DATABASES, TABLES, QueryError, load_config
from chatlog_core.query import connect, select
from voice_asr_core import prepare as prepare_voice_schema

USER = 'chatlog_reader'
HOST = '127.0.0.1'


def validate_grants(rows):
    expected = {(db, table) for db in DATABASES.values() for table in TABLES}
    actual = set()
    for row in rows:
        grant = next(iter(row.values()))
        prefix = grant.split(' TO ', 1)[0].replace('`', '')
        if 'WITH GRANT OPTION' in grant or 'WITH ADMIN OPTION' in grant:
            raise QueryError('excess_privileges', '已有账号包含额外权限，停止安装')
        if prefix == 'GRANT USAGE ON *.*':
            continue
        match = re.fullmatch(r'GRANT SELECT ON ([a-z_]+)\.([a-z_]+)', prefix)
        if not match or (match[1], match[2]) not in expected:
            raise QueryError('excess_privileges', '已有账号包含额外权限或角色，停止安装')
        actual.add((match[1], match[2]))
    return actual


def exclusive_write(path, text, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(mode)


def install():
    launcher = Path.home() / 'bin/chatlog'
    body = '#!/bin/sh\n# Managed by wechat-chatlog installer.\nexec ' + shlex.quote(str(ROOT / '.venv/bin/python')) + ' ' + shlex.quote(str(ROOT / 'scripts/chatlog.py')) + ' "$@"\n'
    if launcher.is_symlink() or (launcher.exists() and launcher.read_text() != body):
        raise QueryError('launcher_conflict', '~/bin/chatlog 已存在且不是本项目启动器；未覆盖')
    if CONFIG.is_symlink():
        raise QueryError('config_conflict', '配置路径是符号链接；未覆盖')
    old = CONFIG.exists()
    config = load_config() if old else dict(user=USER, host=HOST, port=int(os.environ.get('WX_MYSQL_PORT', 3306)), password=secrets.token_urlsafe(36))
    if os.environ.get('WX_MYSQL_HOST', '127.0.0.1') not in ('127.0.0.1', 'localhost'):
        raise QueryError('local_only', '安装只支持本机 MySQL')
    if old and config['port'] != int(os.environ.get('WX_MYSQL_PORT', 3306)):
        raise QueryError('port_conflict', '已有配置的 MySQL 端口与安装目标不一致')
    for db in DATABASES.values():
        with admin.connect(db) as schema_connection:
            prepare_voice_schema(schema_connection)
    with admin.connect() as c:
        existing = select(c, 'SELECT User,Host FROM mysql.user WHERE User=%s', [USER])
        if existing and (not old or any(row['Host'] != HOST for row in existing)):
            raise QueryError('account_conflict', '同名数据库账号已存在且不能确认由本安装器管理；未修改')
        if existing:
            validate_grants(select(c, 'SHOW GRANTS FOR %s@%s', [USER, HOST]))
            # Verify the saved credential before granting anything.
            check = connect(config, DATABASES['wechat'])
            check.close()
        else:
            with c.cursor() as cur:
                cur.execute('CREATE USER %s@%s IDENTIFIED BY %s', [USER, HOST, config['password']])
            if not old:
                try:
                    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    CONFIG.parent.chmod(0o700)
                    exclusive_write(CONFIG, json.dumps(config, indent=2) + '\n', 0o600)
                except Exception:
                    with c.cursor() as cur:
                        cur.execute('DROP USER %s@%s', [USER, HOST])
                    raise
        with c.cursor() as cur:
            for db in DATABASES.values():
                for table in TABLES:
                    cur.execute(f'GRANT SELECT ON `{db}`.`{table}` TO %s@%s', [USER, HOST])
        actual = validate_grants(select(c, 'SHOW GRANTS FOR %s@%s', [USER, HOST]))
        if len(actual) != len(DATABASES) * len(TABLES):
            raise QueryError('grant_incomplete', '只读授权尚未完成，可重跑安装器')
    for database in DATABASES.values():
        check = connect(config, database)
        try:
            select(check, 'SELECT id FROM messages LIMIT 1')
        finally:
            check.close()
    launcher.parent.mkdir(exist_ok=True)
    if not launcher.exists():
        exclusive_write(launcher, body, 0o755)
    else:
        launcher.chmod(0o755)
    print('已安装 ' + str(launcher))
    print('只读账号已验证：仅两个聊天库所需表的 SELECT 权限。')
    print('运行 chatlog --help 或 chatlog capabilities --json。')
    print('MCP 图片回传默认关闭；需要 AI 看本机图片时运行 scripts/chatlog_media_consent.py grant。')


if __name__ == '__main__':
    try:
        install()
    except QueryError as exc:
        print(exc.detail['message'], file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print('安装失败（' + type(exc).__name__ + '）；未输出数据库凭据。', file=sys.stderr)
        raise SystemExit(1)
