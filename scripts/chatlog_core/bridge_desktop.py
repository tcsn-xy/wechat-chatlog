"""Use the installed desktop's queue command; never starts a second agent."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import subprocess

from .common import QueryError

DESKTOP = Path('/Applications/ChatGPT.app')
CLI = DESKTOP / 'Contents/Resources/codex-cli/bin/codex'


class Desktop:
    def __init__(self, home=None, cli=CLI):
        self.home = Path(home or Path.home() / '.codex')
        self.cli = Path(cli)

    def catalog(self):
        paths = [p for p in self.home.glob('state_*.sqlite') if re.fullmatch(r'state_\d+\.sqlite', p.name)]
        if not paths:
            raise QueryError('desktop_catalog_missing', '未找到桌面 Codex 本机会话目录')
        path = max(paths, key=lambda p: int(p.stem.split('_')[1]))
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise QueryError('desktop_catalog_unsafe', '会话目录文件所有者或路径异常')
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3) as c:
            c.execute('PRAGMA query_only=ON')
            c.row_factory = sqlite3.Row
            columns={r[1] for r in c.execute('PRAGMA table_info(threads)')}
            title="COALESCE(NULLIF(name,''),title)" if 'name' in columns else 'title'
            rows = [dict(r) for r in c.execute('SELECT id,'+title+' title,cwd,updated_at FROM threads WHERE archived=0 AND agent_role IS NULL ORDER BY updated_at DESC')]
        threads = []
        for row in rows:
            cwd = Path(row['cwd'])
            if cwd.is_absolute() and cwd.is_dir():
                # Historical title columns can hold the entire first prompt.
                row['title']=re.sub(r'(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{12,}','[redacted]',row['title'].splitlines()[0] if row['title'] else row['id'])[:100]
                threads.append(dict(row, workspace=str(cwd.resolve())))
        return dict(workspaces=sorted({r['workspace'] for r in threads}), threads=threads)

    def binding(self, thread_id, workspace):
        rows = [r for r in self.catalog()['threads'] if r['id'] == thread_id]
        if len(rows) != 1 or rows[0]['workspace'] != str(Path(workspace).resolve()):
            raise QueryError('desktop_binding_mismatch', '所选会话不存在、已归档或不属于所选工作空间')
        return rows[0]

    def running(self):
        if not self.cli.is_file():
            return False
        rows = subprocess.run(['ps', '-axo', 'comm='], capture_output=True, text=True, timeout=5, check=True).stdout.splitlines()
        return any(r.strip() == str(DESKTOP / 'Contents/MacOS/ChatGPT') for r in rows)

    def submit(self, thread_id, workspace, prompt):
        self.binding(thread_id, workspace)
        if not self.running():
            raise QueryError('desktop_not_running', 'ChatGPT 桌面 app 未运行；未启动独立 Codex')
        # Do not pass model/sandbox/profile overrides: retain the chosen thread.
        try:
            result = subprocess.run([str(self.cli), 'queue', '--thread', thread_id,
                                     '--message', prompt], cwd=workspace, capture_output=True,
                                    text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return dict(status='unknown')
        match = re.fullmatch(r'Queued message ([0-9a-f-]{36}) for thread ([0-9a-f-]{36})\.\s*', result.stdout)
        if result.returncode or not match or match[2] != thread_id:
            return dict(status='not_sent' if result.returncode else 'unknown')
        return dict(status='desktop_queued', native_message_id=match[1])
