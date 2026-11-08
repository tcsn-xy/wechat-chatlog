#!/usr/bin/env python3
"""Mount a local adapter in an existing DSH web profile; preserve all settings."""
import argparse
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import sys
import hashlib
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'lib')]
from chatlog_core.bridge_store import Store
from chatlog_core.bridge_dsh import CONFIG
from chatlog_core.common import QueryError


def install(home=None,profile='web'):
    home=Path(home or Path.home()/'.dsh').resolve()
    if profile!='web':raise QueryError('unsupported_dsh_profile','当前适配只挂载现有 web profile')
    patch=home/'profiles'/profile/'cordis.patch.yml'
    if not patch.is_file() or patch.is_symlink():raise QueryError('dsh_profile_missing','未找到现有 dsh web 配置')
    root=Store().root/'dsh-web';root.mkdir(mode=0o700,exist_ok=True);root.chmod(0o700)
    token=root/'ipc-token'
    if not token.exists():
        fd=os.open(token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
    if token.is_symlink() or token.stat().st_uid!=os.getuid() or token.stat().st_mode&0o077:raise QueryError('unsafe_dsh_token','dsh IPC 密钥权限不安全')
    settings=dict(profile=profile,home=str(home),socket_path=str(root/'bridge.sock'),token_file=str(token))
    CONFIG.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    source=(ROOT/'integrations/dsh-chatlog-bridge/index.mjs').read_bytes()
    settings['socket_path']=str(root/('bridge-'+hashlib.sha256(source).hexdigest()[:16]+'.sock'))
    module=root/('adapter-'+hashlib.sha256(source).hexdigest()[:20]+'.mjs')
    if not module.exists():module.write_bytes(source);module.chmod(0o600)
    elif module.read_bytes()!=source:raise QueryError('dsh_adapter_changed','本机适配副本摘要不一致')
    original=patch.read_bytes();marker='# chatlog-local-bridge managed mount'
    row=dict(id='chatlog-local-bridge-'+hashlib.sha256(source).hexdigest()[:12],name=str(module),config=dict(profile=profile,socketPath=settings['socket_path'],tokenFile=str(token)))
    lines=original.decode().splitlines(keepends=True)
    fragment=marker+'\n- insert: '+json.dumps([row],ensure_ascii=False)+'\n'
    if marker in original.decode():
        index=next(i for i,s in enumerate(lines) if s.strip()==marker)
        if index+1>=len(lines) or not lines[index+1].startswith('- insert: '):raise QueryError('dsh_mount_conflict','适配配置片段已被改动')
        updated=''.join(lines[:index])+fragment+''.join(lines[index+2:])
    else:updated=original.decode()+'\n'+fragment
    if updated.encode()!=original:
        backup=ROOT/'.runtime/dsh-bridge-setup';backup.mkdir(parents=True,exist_ok=True,mode=0o700)
        target=backup/('cordis.patch-'+str(time.time_ns())+'.yml');target.write_bytes(original);target.chmod(0o600)
        if patch.read_bytes()!=original:raise QueryError('dsh_config_changed','dsh 配置并发变化，停止写入')
        fd,temp=tempfile.mkstemp(prefix='.chatlog-',dir=patch.parent)
        with os.fdopen(fd,'wb') as f:f.write(updated.encode())
        os.chmod(temp,0o600);os.replace(temp,patch)
    if CONFIG.is_symlink():raise QueryError('unsafe_dsh_config','dsh 适配配置不能是符号链接')
    CONFIG.write_text(json.dumps(settings,ensure_ascii=False,indent=2));CONFIG.chmod(0o600)
    return dict(configured=True,profile=profile,socket_path=settings['socket_path'],hot_reload_requested=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--profile',default='web');args=p.parse_args()
    print(json.dumps(install(profile=args.profile),ensure_ascii=False))
