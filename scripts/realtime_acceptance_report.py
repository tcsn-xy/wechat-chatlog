#!/usr/bin/env python3
"""Read-only production evidence for realtime synchronization."""
import datetime as dt
import json
from pathlib import Path
import re
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'lib'))
from mysql_store import conn as my,realtime_bridge as bridge
from localio import atomic_json


def main():
    evidence=dict(at=dt.datetime.now().isoformat(),platforms={})
    for platform,db in [('wechat','wx_chat'),('wecom','wecom_chat')]:
        with my.connect(db) as c:
            status=bridge.read(platform)
            checks=dict(messages=my.scalar(c,'SELECT COUNT(*) FROM messages'),
                missing_raw=my.scalar(c,'SELECT COUNT(*) FROM messages m LEFT JOIN message_raw r ON r.message_pk=m.id WHERE r.message_pk IS NULL'),
                missing_links=my.scalar(c,'SELECT COUNT(*) FROM messages m WHERE has_link=1 AND NOT EXISTS (SELECT 1 FROM message_links l WHERE l.message_pk=m.id)'),
                dangling_media=my.scalar(c,'SELECT COUNT(*) FROM media_items i LEFT JOIN messages m ON m.id=i.message_pk WHERE i.message_pk IS NOT NULL AND m.id IS NULL'))
            sources=my.query(c,'SELECT COUNT(*) sources,SUM(bootstrap) bootstrap_pending,SUM(checked_rows) checked_rows,MAX(last_full_at) latest_source_check FROM rt_sources')[0]
            evidence['platforms'][platform]=dict(status=status,checks=checks,sources=sources)
    log=ROOT/'.runtime/realtime-all-tests.log'
    text=log.read_text() if log.exists() else '';match=re.search(r'Ran (\d+) tests',text)
    evidence['tests']=dict(count=int(match.group(1)) if match else None,passed=bool(re.search(r'^OK$',text,re.M)))
    evidence=json.loads(json.dumps(evidence,default=lambda v:v.isoformat() if hasattr(v,'isoformat') else int(v)))
    atomic_json(bridge.STATE/'acceptance.json',evidence)
    lines=['# 准实时入库验收','',f"检查时间：{evidence['at']}。",'',
        '真正 launchd 观察阶段已验证微信、企微数据库/WAL 直读、复制验证和文件变化捕获；观察证据在本机 `.runtime/chat-watch-sync/observation-acceptance.json`。','',
        '| 平台 | 模式 | 消息数 | 新增延迟样本 | P95 秒 | 全量核验 | 原文缺失 | 链接缺失 | 媒体孤立关联 |',
        '|---|---|---:|---:|---:|---|---:|---:|---:|']
    for platform,v in evidence['platforms'].items():
        s=v['status'];c=v['checks'];audit=s.get('last_full_success') or ('进行中，剩余 %s 个来源'%s.get('full_sources','未知'))
        p95=s.get('latency_p95_seconds');latency=str(p95) if p95 is not None else '样本不足，待验证'
        lines.append(f"| {platform} | {s.get('mode')} | {c['messages']:,} | {s.get('latency_samples',0)} | {latency} | {audit} | {c['missing_raw']} | {c['missing_links']} | {c['dangling_media']} |")
    lines+=['','延迟以本机相关文件变化首次被观察到、至事务提交为口径；使用单调时钟。首次启动追赶和历史扫描不计入，样本至少 20 条才报告 P95。没有发送测试聊天消息。','',
        f"隔离测试：{evidence['tests']['count']} 项，{'通过' if evidence['tests']['passed'] else '未确认'}。日志 `.runtime/realtime-all-tests.log`；全部使用虚构数据和 repairtest_* 临时库。",'',
        '覆盖事务回滚/游标、低 ID 历史、压缩正文、新群映射、分片、WAL 活跃写入和轮换、源替换/结构变化、旧版本覆盖、重复 URL、权限失败、MySQL 不可用、媒体锁隔离、连接释放及既有链路回归。','',
        '正式兼容修复：移除了企微来源登记表原有、会挤掉多会话记录的 uk_source 唯一约束；未删除原消息或媒体。SQLite 连接按批关闭，不提高系统文件句柄限制。','',
        '每小时入口在实时健康时提交分批全量核验；停用/降级后原路径继续。媒体文件仍每小时，caption 独立运行。未更新 HTML，未重建正式库，未清理恢复备份。','',
        '尚在执行的首次全量核验和样本不足的平台不标为验收完成；后台持续处理，重新运行本脚本可更新证据。']
    output=ROOT/'.runtime/realtime-acceptance.md';output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text('\n'.join(lines)+'\n')
    print(json.dumps(evidence,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
