"""Authenticated local IPC to an extension inside the existing DSH web host."""
from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import socket
import stat
from .common import QueryError

CONFIG = Path.home()/'.config/chatlog/dsh.json'


class Dsh:
    def __init__(self, config_path=CONFIG):
        self.path=Path(config_path)

    def settings(self):
        try:
            info=self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:raise ValueError()
            value=json.loads(self.path.read_text());token=Path(value['token_file']);s=token.lstat()
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o600:raise ValueError()
            return value,token.read_text().strip()
        except (OSError,ValueError,KeyError):
            raise QueryError('dsh_setup_required','dsh 本机适配尚未配置') from None

    def call(self, method, params=None, timeout=15):
        config,token=self.settings()
        try:
            info=Path(config['socket_path']).lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:raise ValueError()
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(timeout);s.connect(config['socket_path'])
                s.sendall((json.dumps(dict(id=secrets.token_hex(8),method=method,params=params or {},token=token),ensure_ascii=False)+'\n').encode())
                data=bytearray()
                while b'\n' not in data:
                    part=s.recv(65536)
                    if not part:raise ValueError()
                    data.extend(part)
                    if len(data)>4*1024*1024:raise ValueError()
            reply=json.loads(data)
            if 'error' in reply:
                code=reply['error'].get('code','dsh_call_failed')
                raise QueryError('dsh_call_failed','dsh 本机调用失败',reason=code)
            return reply['result']
        except (OSError,ValueError,KeyError):
            raise QueryError('dsh_unavailable','dsh 本机适配不可用；未切换到其他后端') from None

    def running(self):
        try:return self.call('health',timeout=2).get('ready') is True
        except QueryError:return False

    def catalog(self):
        value=self.call('catalog')
        return dict(workspaces=[w['path'] for w in value['workspaces']],workspace_records=value['workspaces'],threads=[dict(id=s['id'],title=s['title'],workspace=s.get('workspace'),running=s.get('running')) for s in value['sessions']])

    def binding(self, thread_id, workspace):
        self.call('binding',dict(session_id=thread_id,workspace=str(Path(workspace).resolve())))
        return dict(id=thread_id,workspace=str(Path(workspace).resolve()))

    def submit(self, thread_id, workspace, prompt, request_id=None):
        try:
            result=self.call('prompt',dict(session_id=thread_id,workspace=workspace,text=prompt,request_id=request_id or 'local_'+secrets.token_hex(16)))
        except QueryError as exc:
            # Once a prompt call was attempted, transport loss is uncertain.
            return dict(status='unknown',native_message_id=None) if exc.detail['code']=='dsh_unavailable' else dict(status='dsh_rejected')
        return dict(status='dsh_queued' if result.get('accepted') else 'unknown',native_message_id=result.get('request_id'))
