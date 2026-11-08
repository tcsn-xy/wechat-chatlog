"""Local management daemon and CLI. Does not supervise or restart Codex."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
import socket

from .common import ROOT, QueryError
from .bridge import Bridge
from .bridge_store import STATE, Store
from .bridge_web import server

LABEL = 'local.chatlog.codex-bridge'
PORT = 27146
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def launch_config(store):
    return dict(Label=LABEL, ProgramArguments=[str(ROOT / '.venv/bin/python'), '-B', str(ROOT / 'scripts/chatlog_bridge.py'), 'serve'],
                WorkingDirectory=str(ROOT), RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
                StandardOutPath=str(store.root / 'service.log'), StandardErrorPath=str(store.root / 'service.err'))


def install(store):
    value = plistlib.dumps(launch_config(store))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if PLIST.is_symlink():
        raise QueryError('bridge_service_conflict', '服务文件不能是符号链接')
    if PLIST.exists() and plistlib.loads(PLIST.read_bytes()).get('Label') != LABEL:
        raise QueryError('bridge_service_conflict', '已有服务配置不匹配')
    PLIST.write_bytes(value)
    PLIST.chmod(0o600)
    store.admin_token()
    for name in ('service.log', 'service.err'):
        fd = os.open(store.root / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    target = f'gui/{os.getuid()}'
    existing = subprocess.run(['launchctl', 'print', target + '/' + LABEL], capture_output=True)
    if existing.returncode == 0:
        return dict(status='installed', already_running=True)
    result = subprocess.run(['launchctl', 'bootstrap', target, str(PLIST)], capture_output=True)
    if result.returncode:
        raise QueryError('bridge_service_start_failed', '本机 LaunchAgent 启动失败；未启动独立 Codex')
    return dict(status='installed', url=f'http://127.0.0.1:{PORT}')


def serve(bridge, port=PORT):
    from localio import task_lock, atomic_json
    stop = threading.Event()
    with task_lock(bridge.store.root / 'daemon.lock'):
        httpd = server(bridge, port)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        bridge.async_health=True;wake=threading.Event()
        from runtime_performance import settings
        performance=settings()
        notify_path=bridge.store.root/'notify.sock'
        if notify_path.exists():notify_path.unlink()
        notify=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);notify.bind(str(notify_path));notify_path.chmod(0o600);notify.settimeout(1)
        def notices():
            while not stop.is_set():
                try:bridge.notice(json.loads(notify.recv(65536)));wake.set()
                except (OSError,ValueError,TypeError):pass
        def health():
            monitor_health(bridge,stop,wake,performance['health_seconds'])
        threading.Thread(target=notices,daemon=True).start();threading.Thread(target=health,daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        try:
            while not stop.is_set():
                bridge.poll()
                atomic_json(bridge.store.root / 'runtime.json', dict(pid=os.getpid(), heartbeat=time.time(), port=httpd.server_port, platforms=bridge.health))
                wake.wait(performance['poll_seconds']);wake.clear()
        finally:
            httpd.shutdown()
            httpd.server_close()
            notify.close();notify_path.unlink(missing_ok=True)
            if hasattr(bridge.feed,'close'):bridge.feed.close()


def monitor_health(bridge,stop,wake,interval=15):
    from . import sending
    while not stop.is_set():
        paused=True
        try:
            paused=bridge.store.config()['paused']
            if not paused:
                bridge.sender_health=sending.status();bridge.sender_health_at=time.monotonic();wake.set()
        except Exception:bridge.sender_health={};paused=False
        ready=bool(bridge.sender_health) and all(v.get('ready') for v in bridge.sender_health.values())
        # Transient child-process races must not hold new inputs for an entire
        # healthy probe interval. Sending still performs a fresh full preflight.
        stop.wait(interval if paused or ready else .5)


def live_status(store):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/state', headers={'Authorization': 'Bearer ' + store.admin_token()})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as response:
            return json.load(response)
    except OSError:
        return dict(service='not_running', config=store.config())


def proxy_reply(token,text,attachments=None):
    session=os.environ.get('DSH_SESSION_ID') or os.environ.get('CODEX_THREAD_ID')
    if not session:raise QueryError('bridge_thread_mismatch','回复命令必须由接收问题的后端会话调用')
    body=json.dumps(dict(text=text,attachments=attachments,session_id=session),ensure_ascii=False).encode()
    request=urllib.request.Request(f'http://127.0.0.1:{PORT}/api/reply',data=body,headers={'Content-Type':'application/json','Authorization':'Bearer '+token},method='POST')
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=65) as response:return json.load(response)
    except urllib.error.HTTPError as exc:
        try:error=json.load(exc)
        except ValueError:error={}
        raise QueryError(error.get('code','bridge_reply_rejected'),error.get('message','桥接回复被拒绝')) from None
    except (OSError,ValueError):raise QueryError('bridge_reply_unknown','本机回复结果未知；不要换凭据或正文重发') from None


def main(argv=None):
    sys.path.insert(0, str(ROOT / 'lib'))
    parser = argparse.ArgumentParser(prog='chatlog bridge', description='本机微信 / 企微到桌面 Codex 的授权桥接。工作排队由 Codex 管理。')
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('serve', 'install', 'open', 'status','pause','resume'):
        child = sub.add_parser(name)
        child.add_argument('--json', action='store_true')
    for name in ('workspaces','sessions'):
        child=sub.add_parser(name);child.add_argument('--backend',choices=['codex','dsh'],required=True);child.add_argument('--workspace');child.add_argument('--json',action='store_true')
    child=sub.add_parser('bind');child.add_argument('--person',required=True);child.add_argument('--backend',choices=['codex','dsh'],required=True);child.add_argument('--workspace',required=True);child.add_argument('--session',required=True);child.add_argument('--json',action='store_true')
    people=sub.add_parser('people');people_sub=people.add_subparsers(dest='people_action',required=True)
    for name in ('list','add','remove','enable','disable'):
        child=people_sub.add_parser(name);child.add_argument('--json',action='store_true')
        if name=='add':
            child.add_argument('--self',dest='owner_self',action='store_true');child.add_argument('--name');child.add_argument('--wechat');child.add_argument('--wecom')
        elif name!='list':
            child.add_argument('person')
            if name=='enable':child.add_argument('--confirm-identities',action='store_true')
    backend=sub.add_parser('backend');backend_sub=backend.add_subparsers(dest='backend_action',required=True)
    child=backend_sub.add_parser('use');child.add_argument('backend',choices=['codex','dsh']);group=child.add_mutually_exclusive_group(required=True);group.add_argument('--all',action='store_true');group.add_argument('--person');child.add_argument('--json',action='store_true')
    child=sub.add_parser('perf');child.add_argument('--seconds',type=int,default=60);child.add_argument('--json',action='store_true')
    reply = sub.add_parser('reply', help='仅在接收问题的指定 Codex 会话中回复原聊天')
    reply.add_argument('token')
    from .send_payload import add_payload_arguments
    add_payload_arguments(reply)
    reply.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action=='reply' and not os.environ.get('CHATLOG_BRIDGE_STATE'):
            result=proxy_reply(args.token,args.text,args.attachments)
            print(json.dumps(result,ensure_ascii=False));return 0 if result.get('ok') else (5 if result.get('partial') else 4)
        store = Store(os.environ.get('CHATLOG_BRIDGE_STATE', STATE) if args.action == 'reply' else STATE)
        if args.action == 'serve':
            serve(Bridge(store))
            return 0
        if args.action == 'install':
            result = install(store)
        elif args.action == 'open':
            if live_status(store).get('service') == 'not_running':
                raise QueryError('bridge_not_running', '请先运行 chatlog bridge install')
            webbrowser.open(f'http://127.0.0.1:{PORT}/#token=' + store.admin_token())
            result = dict(status='opened', url=f'http://127.0.0.1:{PORT}')
        elif args.action == 'status':
            result = live_status(store)
        elif args.action=='perf':
            if not 1<=args.seconds<=3600:raise QueryError('invalid_duration','测量时长需要在 1–3600 秒之间')
            from .bridge_perf import measure
            result=measure(args.seconds)
            result['dispatch_latency']=store.latency()
        elif args.action!='reply':
            from .bridge_manage import manage
            result=manage(Bridge(store),args)
        else:
            result = Bridge(store).reply(args.token, args.text, attachments=args.attachments)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if args.action != 'reply' or result.get('ok') else (5 if result.get('partial') else 4)
    except QueryError as error:
        print(json.dumps(dict(ok=False, error=error.detail), ensure_ascii=False))
        return 2 if error.detail['code'] not in ('dsh_unavailable','dsh_call_failed') else 4


if __name__ == '__main__':
    raise SystemExit(main())
