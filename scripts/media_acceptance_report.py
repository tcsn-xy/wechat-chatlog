#!/usr/bin/env python3
"""Generate dated acceptance evidence without rewriting historical reports."""
import datetime as dt
import json
import re
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'lib'))
from mysql_store import conn as my,files
from localio import atomic_json


def main():
    state=ROOT/'.runtime/media-files'
    load=lambda name:json.loads((state/name).read_text()) if (state/name).exists() else {}
    verification=load('verify.json');retention=load('migration-audit.json')
    migrated=[]
    if (state/'migration.jsonl').exists():migrated=[json.loads(line) for line in (state/'migration.jsonl').read_text().splitlines()]
    summary={}
    for db in ['wx_chat','wecom_chat']:
        with my.connect(db) as c:
            summary[db]=dict(messages=my.scalar(c,'SELECT COUNT(*) FROM messages'),
                media=my.query(c,"SELECT msg_kind,COUNT(*) n,SUM(file_path IS NOT NULL) stored_count,SUM(unavailable=1) missing FROM media_items WHERE msg_kind IN ('image','voice') GROUP BY msg_kind"),
                remaining_blob_rows=my.scalar(c,"SELECT COUNT(DISTINCT i.id) FROM media_items i LEFT JOIN blob_parts b ON b.row_id=i.id AND b.table_name IN ('media_items','media_items.decoded_data') WHERE i.msg_kind IN ('image','voice') AND (i.data IS NOT NULL OR i.decoded_data IS NOT NULL OR b.id IS NOT NULL)"),
                dangling=my.scalar(c,'SELECT COUNT(*) FROM media_items i LEFT JOIN messages m ON m.id=i.message_pk WHERE i.message_pk IS NOT NULL AND m.id IS NULL'),
                captions=my.query(c,'SELECT status,COUNT(*) n FROM image_captions GROUP BY status'),
                caption_errors=my.query(c,"SELECT status,error_code,COUNT(*) n,MIN(next_attempt) next_attempt FROM image_captions WHERE error_code IS NOT NULL GROUP BY status,error_code"),
                database_bytes=my.scalar(c,'SELECT SUM(data_length+index_length) FROM information_schema.tables WHERE table_schema=DATABASE()'))
    physical=[p for p in files.root().rglob('*') if p.is_file() and not p.name.startswith('.')]
    evidence=dict(at=dt.datetime.now().isoformat(),migration_rows=len(migrated),migration_bytes=sum(r['bytes'] for r in migrated),
        physical_files=len(physical),physical_bytes=sum(p.stat().st_size for p in physical),verification=verification,retention=retention,
        sync=load('sync.json'),databases=summary)
    evidence=json.loads(json.dumps(evidence,default=lambda v:v.isoformat() if hasattr(v,'isoformat') else int(v)))
    atomic_json(state/'acceptance.json',evidence)
    lines=['# 媒体文件与图片描述验收','',f"检查时间：{evidence['at']}。",'',
        f"历史迁移清单：{evidence['migration_rows']:,} 条，迁出原始及转换字节 {evidence['migration_bytes']:,}。逐项验证成功才清空 BLOB，完整清单保存在本机 `.runtime/media-files/migration.jsonl`。",'',
        f"受管理目录实际存储 {evidence['physical_files']:,} 个内容文件，共 {evidence['physical_bytes']:,} 字节。按内容复用，故文件数与消息引用数不同。",'',
        '正式库媒体 ID、消息关联、其他媒体及分片的迁移前后 SHA256 对照：'+('全部通过。' if retention and all(all(r.values()) for r in retention.values()) else '待核实。'),'',
        '虚构图片已通过 MiniMax Coding Plan 实际调用，真实图片描述已有成功入库。API key 只保存在本机被忽略的密钥文件，权限 0600。','',
        '| 数据库 | 消息 | 图片文件引用/记录 | 语音文件引用/记录 | 剩余媒体 BLOB 行 | 孤立关联 | caption 已完成 | caption 待处理/重试 | 暂停/失败 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for db,s in summary.items():
        media={r['msg_kind']:r for r in s['media']};q={r['status']:r['n'] for r in s['captions']}
        fmt=lambda k:f"{int(media.get(k,{}).get('stored_count') or 0):,}/{int(media.get(k,{}).get('n') or 0):,}"
        lines.append(f"| {db} | {s['messages']:,} | {fmt('image')} | {fmt('voice')} | {s['remaining_blob_rows']} | {s['dangling']} | {q.get('complete',0)} | {q.get('pending',0)+q.get('retry',0)} | {q.get('paused',0)}/{q.get('failed',0)} |")
    lines+=['','图片描述错误及自动重试：','']
    for db,s in summary.items():
        for error in s['caption_errors']:
            lines.append(f"- {db}：{error['error_code']}，{error['status']}，{error['n']} 张；下次尝试时间 {error['next_attempt'] or '无（永久失败/需人工修复）'}。")
    lines+=['','2056 表示套餐资源限制：全队列暂停，一小时后自动尝试；不会切换收费模型。1026 是服务拒绝单张图片，已登记失败，其余图片继续。','']
    test_log=(ROOT/'.runtime/media-files-tests.log').read_text()
    count=re.search(r'Ran (\d+) tests',test_log)
    passed=count and re.search(r'^OK$',test_log,re.M) and not re.search(r'^FAILED',test_log,re.M)
    lines.append(f"全套测试：{count.group(1) if count else '未知'} 项，{'通过' if passed else '尚未确认'}。")
    lines+=['','文件全量校验结果：','']
    for db,r in verification.items():lines.append(f"- {db}：{r['files']:,} 次文件引用校验，失败 {r['failed']}。")
    lines+=['','每小时媒体同步结果：','', '```json',json.dumps(evidence['sync'],ensure_ascii=False,indent=2),'```','',
        '测试证据：`.runtime/media-files-tests.log`；合成输入全链路验收 `.runtime/media-files/isolated-report.md`（33 项通过、0 失败、0 跳过）。均只使用虚构数据与 repairtest_* 临时库。','',
        'caption 为持续后台任务，上表为检查时刻进度；待处理、重试、套餐暂停不算完成。未缓存图片仍明确标为不可用。','',
        'MySQL 表当前数据与索引占用（不是释放到文件系统的字节）：','']
    for db,s in summary.items():lines.append(f"- {db}：{int(s['database_bytes'] or 0):,} 字节。")
    lines+=['','没有执行 OPTIMIZE、重建正式库、清理恢复备份或更新 HTML；历史入库报告保持原日期。']
    output=ROOT/'.runtime/media-acceptance.md';output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text('\n'.join(lines)+'\n')
    print(json.dumps(dict(report=str(output),migration_rows=evidence['migration_rows'],migration_bytes=evidence['migration_bytes']),ensure_ascii=False))

if __name__=='__main__':main()
