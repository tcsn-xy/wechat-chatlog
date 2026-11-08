"""Bounded process measurements; no chat bodies, credentials or shell commands."""
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import time

LABELS={'bridge':'local.chatlog.codex-bridge','wechat_sender':'local.wxexport.sender','wecom_sender':'local.chatlog.wecom-sender','sync':'local.wxexport.realtime-sync','dsh':'com.deepseek.dsh.web'}


def cpu_seconds(value):
    days,_,clock=value.rpartition('-')
    return (int(days)*86400 if days else 0)+sum(float(v)*60**i for i,v in enumerate(reversed(clock.split(':'))))


def measure(seconds=60):
    targets={}
    for name,label in LABELS.items():
        out=subprocess.run(['launchctl','list',label],capture_output=True,text=True).stdout
        pid=re.search(r'"PID"\s*=\s*(\d+)',out)
        if pid:targets[name]=int(pid[1])
    rows=subprocess.run(['ps','-axo','pid=,comm='],capture_output=True,text=True).stdout.splitlines()
    for line in rows:
        parts=line.strip().split(None,1)
        if len(parts)!=2:continue
        pid,cmd=parts
        if cmd.endswith('/mysqld'):targets['mysql']=int(pid)
        if cmd.endswith('/WeChatRun.app/Contents/MacOS/WeChat'):targets['wechat_client_main']=int(pid)
        if cmd.endswith('/WeComSigned.app/Contents/MacOS/企业微信'):targets['wecom_client_main']=int(pid)
    samples={p:[] for p in targets.values()};start=time.monotonic()
    while True:
        if samples:
            out=subprocess.run(['ps','-p',','.join(map(str,samples)),'-o','pid=,time=,rss='],capture_output=True,text=True).stdout
            for line in out.splitlines():
                pid,cpu,rss=line.split();samples[int(pid)].append((time.monotonic(),cpu_seconds(cpu),int(rss)))
        left=seconds-(time.monotonic()-start)
        if left<=0:break
        time.sleep(min(5,left))
    result={}
    for name,pid in targets.items():
        values=samples[pid]
        if len(values)<2:result[name]=dict(pid=pid,status='insufficient_samples');continue
        result[name]=dict(pid=pid,cpu_one_core_percent=round(100*(values[-1][1]-values[0][1])/(values[-1][0]-values[0][0]),2),rss_mib_mean=round(statistics.mean(v[2] for v in values)/1024,1),rss_mib_max=round(max(v[2] for v in values)/1024,1),samples=len(values))
    return dict(window_seconds=round(time.monotonic()-start,2),cpu_basis='100_percent_is_one_core',memory_basis='RSS_not_exclusive_memory',processes=result)
