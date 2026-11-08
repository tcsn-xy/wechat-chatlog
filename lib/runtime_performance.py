"""Owner-only local performance settings; no credentials or business data."""
import json
import os
from pathlib import Path
import stat

DEFAULTS=dict(background_cpu_budget=0.10,full_scan_hour=3,tail_interval_seconds=300,tail_rows=100,debounce_seconds=0.25,max_debounce_seconds=1.0,poll_seconds=1.0,health_seconds=15.0)


def settings():
    path=Path.home()/'.config/chatlog/performance.json'
    try:
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:return dict(DEFAULTS)
        values=json.loads(path.read_text())
    except (OSError,ValueError):return dict(DEFAULTS)
    result=dict(DEFAULTS)
    limits=dict(background_cpu_budget=(.01,.5),full_scan_hour=(0,23),tail_interval_seconds=(30,86400),tail_rows=(10,2000),debounce_seconds=(.05,3),max_debounce_seconds=(.1,10),poll_seconds=(.2,10),health_seconds=(3,60))
    for key,(lo,hi) in limits.items():
        value=values.get(key)
        if type(value) in (int,float) and lo<=value<=hi:result[key]=value
    return result
