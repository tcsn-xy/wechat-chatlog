"""Local-only semantic index management; deliberately absent from MCP tools."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

from .common import ROOT, QueryError
from .semantic import CONFIG, DIMENSIONS, ENDPOINT, MODEL, STATE, Store, dependencies, read_private, settings, performance

LABEL = 'local.chatlog.semantic-index'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def status(root=None):
    root = Path(root or STATE)
    try:
        store = Store(root)
    except QueryError as exc:
        if exc.detail['code'] == 'semantic_not_ready':
            return dict(configured=CONFIG.exists(), initialized=False, running=False, ready=False)
        raise
    try:
        try:
            runtime = read_private(root/'runtime.json')
        except QueryError:
            runtime = {}
        fresh = time.time() - runtime.get('heartbeat', 0) < 90
        try:
            os.kill(runtime.get('pid', 0) if runtime.get('pid', 0)>0 else -999999, 0)
            running = fresh
        except OSError:
            running = False
        usage = store.db.execute("SELECT COUNT(*),SUM(status='success'),COALESCE(SUM(tokens),0),"
                                 'COALESCE(SUM(duration),0) FROM requests').fetchone()
        coverage = store.coverage()
        return dict(configured=True, initialized=True, running=running,
                    ready=any(p['indexed_messages'] for p in coverage['platforms']),
                    paused=store.get('paused', False), last_error=store.get('last_error'),
                    provider='minimax', model=MODEL, dimensions=DIMENSIONS, coverage=coverage,
                    usage=dict(requests=usage[0], successes=usage[1] or 0, reported_tokens=usage[2],
                               request_seconds=round(usage[3], 3)),
                    ann_indexed_chunks=store.get('ann_rows', 0),
                    pending_vectors=store.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0],
                    request_interval_seconds=1, background_concurrency=1, performance=performance(store),
                    last_batch=store.get('last_batch'),
                    disk_bytes=sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink()),
                    heartbeat=runtime.get('heartbeat'))
    finally:
        store.close()


def launch(root=None):
    from .semantic import private_dir
    root = private_dir(root or STATE)
    settings(); dependencies()
    config = dict(Label=LABEL, ProgramArguments=[str(ROOT/'.venv/bin/python'), '-B',
                  str(ROOT/'scripts/chatlog_semantic.py'), 'worker'], WorkingDirectory=str(ROOT),
                  RunAtLoad=True, KeepAlive=True, ThrottleInterval=10, LowPriorityIO=True, Nice=10,
                  EnvironmentVariables=dict(RAYON_NUM_THREADS='2', OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='1'),
                  StandardOutPath=str(root/'service.log'), StandardErrorPath=str(root/'service.err'))
    if PLIST.is_symlink():
        raise QueryError('semantic_service_conflict', '语义服务配置不能为符号链接')
    if PLIST.exists():
        old = plistlib.loads(PLIST.read_bytes())
        if old.get('Label') != LABEL or old.get('ProgramArguments') != config['ProgramArguments']:
            raise QueryError('semantic_service_conflict', '已有不同的语义服务配置；未覆盖')
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(config)); PLIST.chmod(0o600)
    for name in ('service.log', 'service.err'):
        fd = os.open(root/name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600); os.close(fd)
    target = f'gui/{os.getuid()}'
    exists = subprocess.run(['launchctl', 'print', target+'/'+LABEL], capture_output=True).returncode == 0
    if not exists:
        result = subprocess.run(['launchctl', 'bootstrap', target, str(PLIST)], capture_output=True)
        if result.returncode:
            raise QueryError('semantic_service_failed', 'LaunchAgent 启动失败')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    from .cli import Parser, render
    p = Parser(prog='chatlog semantic', description='本机语义索引管理；不通过 MCP 暴露。')
    sub = p.add_subparsers(dest='action', required=True)
    for action in ('status', 'start', 'pause', 'resume', 'speed', 'worker'):
        child = sub.add_parser(action)
        child.add_argument('--json', action='store_true')
        if action=='speed':
            child.add_argument('--profile',choices=['normal','fast'],required=True,
                               help='fast 扩大批处理；保留单请求并发、每秒至多一次请求和约 10% CPU 预算')
    result = dict(schema_version=1, ok=False, partial=False, data={}, pagination=None, warnings=[], errors=[])
    try:
        args = p.parse_args(argv)
        if args.action == 'worker':
            from .semantic_index import run
            run(); return 0
        if args.action in ('start', 'pause', 'resume', 'speed'):
            settings()
            store = Store(create=args.action == 'start')
            try:
                if args.action=='speed':
                    store.set('performance_profile',args.profile)
                else:
                    store.set('paused', args.action == 'pause')
            finally:
                store.close()
            if args.action in ('start', 'resume'):
                launch()
        result.update(ok=True, data=status())
        code = 0
    except QueryError as exc:
        result['errors'] = [exc.detail]; code = 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        result['errors'] = [dict(code='semantic_failed', message='语义管理失败；未输出私密异常内容')]; code = 4
    render(result, '--json' in argv)
    return code
