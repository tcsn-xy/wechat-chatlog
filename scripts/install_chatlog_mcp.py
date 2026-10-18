#!/usr/bin/env python3
"""Register local read-only chatlog MCP in Codex and Deepseekharness."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import tomllib
import uuid

from chatlog_core.common import ROOT, load_config, QueryError

BEGIN = '# BEGIN managed chatlog MCP'
END = '# END managed chatlog MCP'


def harness_block():
    # JSON strings are valid YAML scalars, including paths containing spaces.
    python = json.dumps(str(ROOT / '.venv/bin/python'), ensure_ascii=False)
    entry = json.dumps(str(ROOT / 'scripts/chatlog_mcp.py'), ensure_ascii=False)
    return f'''{BEGIN}
- insert:
    - id: mcp-chatlog
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: chatlog
        transport: stdio
        command: {python}
        args: [{entry}]
        cwd: {json.dumps(str(ROOT), ensure_ascii=False)}
        toolCallTimeoutMs: 60000
        failOnStartupError: false
{END}
'''


def patch_harness(original):
    block = harness_block()
    if BEGIN in original or END in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            raise QueryError('config_conflict', 'Harness 中存在不完整或重复的 chatlog 配置标记')
        start, end = original.index(BEGIN), original.index(END) + len(END)
        if end < start:
            raise QueryError('config_conflict', 'Harness chatlog 配置标记顺序错误')
        return original[:start] + block.rstrip('\n') + original[end:]
    if 'mcp-chatlog' in original or 'serverName: chatlog' in original:
        raise QueryError('config_conflict', 'Harness 中已有其他 chatlog 配置；未覆盖')
    if original.strip() in ('', '[]'):
        return block
    return original.rstrip() + '\n\n' + block


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.chatlog-mcp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def codex_config_text(original):
    command = str(ROOT / '.venv/bin/python')
    args = [str(ROOT / 'scripts/chatlog_mcp.py')]
    text = original.rstrip() + '\n\n[mcp_servers.chatlog]\ncommand = ' + json.dumps(command, ensure_ascii=False) + '\nargs = ' + json.dumps(args, ensure_ascii=False) + '\n'
    expected = tomllib.loads(original)
    expected.setdefault('mcp_servers', {})['chatlog'] = dict(command=command, args=args)
    if tomllib.loads(text) != expected:
        raise QueryError('config_changed', 'Codex 配置合并校验失败；未覆盖')
    return text


def backup(paths):
    folder = ROOT / '.runtime/chatlog-mcp-backups' / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, mode=0o700)
    for label, path in paths.items():
        if path.exists():
            dest = folder / label
            dest.write_bytes(path.read_bytes())
            dest.chmod(0o600)
    return folder


def install():
    import mcp  # Fail before editing client config if the optional SDK is missing.
    load_config()
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    codex_path = codex_home / 'config.toml'
    harness_home = Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh')))
    harness_path = harness_home / 'cordis.patch.yml'
    have_codex = codex_home.is_dir()
    have_harness = harness_home.is_dir()
    if not have_codex and not have_harness:
        raise QueryError('client_missing', '未找到 Codex 或 Deepseekharness 用户目录；可用 CODEX_HOME 或 DSH_HOME 指定')
    for path in (codex_path, harness_path):
        if path.is_symlink():
            raise QueryError('config_symlink', '客户端配置为符号链接；未覆盖')
    prior = codex_path.read_text() if codex_path.exists() else ''
    config = tomllib.loads(prior) if have_codex and prior else {}
    existing = config.get('mcp_servers', {}).get('chatlog') if have_codex else None
    command, args = str(ROOT / '.venv/bin/python'), [str(ROOT / 'scripts/chatlog_mcp.py')]
    if existing and (existing.get('command') != command or existing.get('args') != args
                     or existing.get('enabled') is False or existing.get('disabled_tools') or existing.get('enabled_tools')):
        raise QueryError('config_conflict', '已有不同或受限的 chatlog 配置；未覆盖')
    old_harness = harness_path.read_text() if harness_path.exists() else ''
    new_harness = patch_harness(old_harness) if have_harness else old_harness
    if (not have_codex or existing) and new_harness == old_harness:
        print('已注册的客户端配置 chatlog MCP；无需修改。')
        return
    saved = backup({'codex-config.toml': codex_path, 'dsh-cordis.patch.yml': harness_path})
    if have_codex and not existing:
        updated = codex_config_text(prior)
        if codex_path.exists() and codex_path.read_text() != prior:
            raise QueryError('config_changed', 'Codex 配置在安装期间变化；未覆盖，可重试')
        atomic_write(codex_path, updated)
    if have_harness and harness_path.exists() and harness_path.read_text() != old_harness:
        raise QueryError('config_changed', 'Harness 配置在安装期间变化；未覆盖，可重试')
    if have_harness and new_harness != old_harness:
        atomic_write(harness_path, new_harness)
    print('已注册本机 chatlog MCP：' + '、'.join(name for name, present in (('Codex', have_codex), ('Deepseekharness', have_harness)) if present))
    print('配置备份：' + str(saved))
    print('已运行的客户端可能需要重新加载 MCP 或重开会话。')


if __name__ == '__main__':
    try:
        install()
    except QueryError as exc:
        print(exc.detail['message'])
        raise SystemExit(1)
    except Exception as exc:
        print('MCP 安装失败（' + type(exc).__name__ + '）；未输出私密配置。')
        raise SystemExit(1)
