#!/usr/bin/env python3
"""Manage a finite, resumable local Qwen voice-transcription batch."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import signal
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'lib'), str(ROOT / 'scripts')]
from mysql_store import conn as my
from localio import atomic_json
from wecom import config as wcconfig
import voice_asr_core as core

LABEL = 'local.wxexport.voice-asr-batch'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
MANIFEST = core.STATE / 'batch-manifest.json'
STOP = False


def state(**values):
    values.update(at=dt.datetime.now(dt.timezone.utc).isoformat(), pid=os.getpid(),
                  max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    atomic_json(core.STATE / 'status.json', values)


def create_manifest(path):
    if path.exists():
        raise core.AudioError('manifest_already_exists')
    now = dt.datetime.now(dt.timezone.utc)
    manifest = dict(batch_id=now.strftime('voice-%Y%m%dT%H%M%SZ'), created_at=now.isoformat(),
                    model_repo=core.CONFIG['model'], items={})
    for platform, database in core.DATABASES.items():
        with my.connect(database) as c:
            manifest['items'][platform] = my.query(c, "SELECT id,platform,account_key,message_pk,file_path,file_sha256,decoded_path,decoded_sha256,bytes,sha1,decoded_sha1,decoded_mime,ext,mime,duration_ms,unavailable FROM media_items WHERE msg_kind='voice' ORDER BY id")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, default=str, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    return dict(batch_id=manifest['batch_id'], counts={p:len(v) for p,v in manifest['items'].items()})


def initialise(path):
    manifest, digest = core.load_manifest(path)
    baseline = core.verify_batch(manifest)
    atomic_json(core.STATE / 'verification-before.json', baseline)
    if baseline['failed']:
        raise core.AudioError('initial_file_verification_failed')
    with core.exclusive_worker() as connections:
        for platform, c in connections.items():
            core.prepare(c)
            core.seed_batch(c, manifest, digest, platform)
    return manifest


def batch_status(manifest, connections=None):
    out = {}
    for platform, db in core.DATABASES.items():
        if connections:
            out[platform] = core.summary(connections[platform], manifest['batch_id'])
            connections[platform].commit()
        else:
            with my.connect(db) as c:
                out[platform] = core.summary(c, manifest['batch_id'])
    return out


def check_links(manifest, connections=None):
    results = {}
    for platform, db in core.DATABASES.items():
        c = connections[platform] if connections else my.connect(db)
        try:
            results[platform] = dict(frozen_items=len(manifest['items'][platform]),
                registered=my.scalar(c, 'SELECT COUNT(*) FROM voice_asr_items WHERE batch_id=%s', (manifest['batch_id'],)),
                media_identity_mismatches=my.scalar(c, '''SELECT COUNT(*) FROM voice_asr_items a LEFT JOIN media_items m ON m.id=a.media_id
                  WHERE a.batch_id=%s AND (m.id IS NULL OR m.msg_kind<>'voice' OR m.platform<>a.platform OR m.account_key<>a.account_key
                  OR NOT(m.message_pk <=> a.message_pk) OR NOT(m.file_sha256 <=> a.original_sha256) OR NOT(m.decoded_sha256 <=> a.decoded_sha256))''', (manifest['batch_id'],)),
                missing_message_links=my.scalar(c, '''SELECT COUNT(*) FROM voice_asr_items a LEFT JOIN messages m ON m.id=a.message_pk
                  WHERE a.batch_id=%s AND a.message_pk IS NOT NULL AND (m.id IS NULL OR m.platform<>a.platform OR m.account_key<>a.account_key)''', (manifest['batch_id'],)))
            c.commit()
        finally:
            if not connections:
                c.close()
    return results


def resolve_formats(manifest):
    # Run with the existing decoder environment, never infer PCM rate from byte length.
    from audio_codec import decode_pcm, to_wav
    from mysql_store import files
    evidence_path = core.STATE / 'format-evidence.json'
    evidence = json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
    found = 0
    with core.exclusive_worker() as connections:
        for platform, rows in manifest['items'].items():
            c = connections[platform]
            for r in rows:
                if r.get('ext') != 'pcm' or r.get('decoded_path'):
                    continue
                original = files.resolve(r['file_path'])
                if files.hashes(original)['sha256'] != r['file_sha256']:
                    raise core.AudioError('file_checksum_mismatch')
                source_path = my.scalar(c, 'SELECT source_path FROM media_items WHERE id=%s AND platform=%s AND account_key=%s',
                                        (r['id'], platform, r['account_key']))
                if not source_path:
                    continue
                source = Path(source_path).with_suffix('').resolve()
                if not source.is_relative_to(Path(wcconfig.DATA)) or not source.is_file():
                    continue
                silk = source.read_bytes()
                pcm = original.read_bytes()
                matches = [rate for rate in (8000,12000,16000,24000)
                           if (decoded := decode_pcm(silk, (rate,))) and decoded[0] == pcm]
                if len(matches) != 1:
                    continue
                rate = matches[0]
                wav = files.publish([to_wav(pcm, rate)], platform, r['account_key'], 'wav')
                witness = files.publish([silk], platform, r['account_key'], 'silk')
                evidence[f"{platform}:{r['id']}"] = dict(account_key=r['account_key'], pcm_sha256=r['file_sha256'],
                    sample_rate=rate, channels=1, sample_bits=16, verification='silk_decode_exact_pcm_match',
                    silk_path=witness['path'], silk_sha256=witness['sha256'], wav_path=wav['path'], wav_sha256=wav['sha256'])
                atomic_json(evidence_path, evidence)
                my.execute(c, "UPDATE voice_transcripts t JOIN voice_asr_items a ON a.transcript_id=t.id SET t.status='pending',t.error_code=NULL WHERE a.batch_id=%s AND a.media_id=%s AND t.status='pending_format'", (manifest['batch_id'], r['id']))
                c.commit()
                found += 1
    return dict(resolved=found, remaining_format_items=sum(row['n'] for stats in batch_status(manifest).values() for row in stats['items'] if row['status']=='pending_format'))


def acceptance(manifest, connections=None):
    verification = core.verify_batch(manifest)
    out = dict(batch_id=manifest['batch_id'], configuration=core.CONFIG,
               generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
               audio_files=verification, links=check_links(manifest, connections),
               counts=batch_status(manifest, connections))
    atomic_json(core.STATE / 'acceptance.json', out)
    created = str(manifest.get('created_at', ''))[:10] or '当前批次'
    lines = ['# 本地语音转写验收（' + created + '）', '',
             '批次：`' + manifest['batch_id'] + '`。Qwen3-ASR-0.6B 8bit / MLX；仅此冻结批次。', '',
             '原始文件与已有 WAV 保留；消息正文、原文与媒体行均不由转写任务修改。', '',
             '| 平台 | 状态 | 关联媒体数 |', '|---|---|---:|']
    for platform, stats in out['counts'].items():
        for row in stats['items']:
            lines.append(f"| {platform} | {row['status']} | {row['n']} |")
    lines += ['', f"文件校验：{verification['unique_files']} 个不同路径，{verification['bytes']} 字节，失败 {verification['failed']} 项。",
              '关联检查：`' + json.dumps(out['links'], ensure_ascii=False) + '`。', '',
              'complete 表示生成了非空转写，不代表人工核验准确率；needs_review 单独标记空结果、重复或异常长度。',
              '模型不提供可校准置信度；不虚构置信分数。时间为 chunk 边界，不是逐字对齐。',
              'PCM 仅在伴随 SILK 解码结果与已存 PCM 逐字节一致时采用该采样率；缺少证据保持 pending_format。失败项保留原文件。',
              '详细机器验收保存在私密 `.runtime/voice-asr/acceptance.json`；报告不包含语音内容。', '']
    (core.STATE / 'acceptance.md').write_text('\n'.join(lines))
    return out


def run(path, limit=0, only_platform=None):
    global STOP
    manifest, digest = core.load_manifest(path)
    core.verify_model()
    for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY', 'DO_NOT_TRACK'):
        os.environ[key] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    from mlx_audio.stt import load
    import mlx.core as mx
    # One model and one GPU inference stream; bounded cache avoids unbounded resident memory.
    mx.set_cache_limit(256 * 1024 * 1024)
    completed = 0
    started = time.monotonic()
    model = None
    failures = 0
    with core.exclusive_worker() as connections:
        for p, c in connections.items():
            existing = my.query(c, 'SELECT manifest_sha256,config_id FROM voice_asr_batches WHERE batch_id=%s', (manifest['batch_id'],))
            if not existing or existing[0] != dict(manifest_sha256=digest, config_id=core.CONFIG_ID):
                raise core.AudioError('batch_configuration_changed')
            core.recover(c, manifest['batch_id'])
            my.execute(c, "UPDATE voice_asr_batches SET status='running',finished_at=NULL WHERE batch_id=%s", (manifest['batch_id'],))
            c.commit()
        state(status='loading_model', batch_id=manifest['batch_id'], counts=batch_status(manifest, connections))
        model = load(str(core.MODEL))
        platforms = [only_platform] if only_platform else list(connections)
        while not STOP and (not limit or completed < limit):
            progressed = False
            for platform in platforms:
                if STOP or (limit and completed >= limit):
                    break
                c = connections[platform]
                job = core.claim(c, manifest['batch_id'])
                if not job:
                    continue
                progressed = True
                pk, snapshots = job
                state(status='running', batch_id=manifest['batch_id'], current_platform=platform,
                      current_job=pk, completed_this_run=completed, elapsed_seconds=round(time.monotonic()-started, 1),
                      peak_mlx_bytes=mx.get_peak_memory(), counts=batch_status(manifest, connections))
                try:
                    audio = None
                    last = None
                    for snapshot in snapshots:
                        try:
                            audio = core.prepare_audio(snapshot)
                            break
                        except core.AudioError as exc:
                            last = exc
                    if audio is None:
                        raise last or core.AudioError('file_missing')
                    if audio['silence']:
                        result = dict(status='silence', text=None, segments=[], language=None, metrics={})
                    else:
                        # Capture library progress/output so speech never lands in worker logs.
                        import contextlib
                        signal.setitimer(signal.ITIMER_REAL, max(120, audio['duration_ms'] / 1000 * 6))
                        try:
                            with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                                generated = model.generate(str(audio['path']), language=None,
                                                           max_tokens=core.CONFIG['max_tokens'], temperature=0, verbose=False)
                        finally:
                            signal.setitimer(signal.ITIMER_REAL, 0)
                        result = core.parse_result(generated, audio['duration_ms'])
                    core.finish(c, pk, audio, result)
                    failures = 0
                except core.AudioError as exc:
                    core.fail(c, pk, str(exc))
                    if str(exc).startswith(('invalid_model_', 'excessive_model_')):
                        failures += 1
                        if failures >= 3:
                            raise core.AudioError('repeated_inference_failure') from None
                except my.pymysql.MySQLError:
                    raise
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    core.fail(c, pk, 'inference_' + type(exc).__name__)
                    failures += 1
                    if failures >= 3:
                        raise core.AudioError('repeated_inference_failure') from None
                finally:
                    mx.clear_cache()
                completed += 1
                print(json.dumps(dict(platform=platform, job_id=pk, processed_this_run=completed)), flush=True)
            if not progressed:
                break
        counts = batch_status(manifest, connections)
        pending = any(r['status'] in ('pending', 'processing') and r['n'] for stats in counts.values() for r in stats['items'])
        if not pending:
            verified = acceptance(manifest, connections)
            if verified['audio_files']['failed'] or any(v['media_identity_mismatches'] or v['missing_message_links'] for v in verified['links'].values()):
                raise core.AudioError('final_verification_failed')
            for c in connections.values():
                my.execute(c, "UPDATE voice_asr_batches SET status='finished',finished_at=NOW() WHERE batch_id=%s", (manifest['batch_id'],))
                c.commit()
        state(status='paused' if STOP else ('pending' if pending else 'finished'), batch_id=manifest['batch_id'],
              completed_this_run=completed, elapsed_seconds=round(time.monotonic()-started, 1),
              peak_mlx_bytes=mx.get_peak_memory(), counts=counts)
    return counts


def manage(action, manifest):
    target = f'gui/{os.getuid()}/{LABEL}'
    if action == 'disable':
        subprocess.run(['launchctl', 'disable', target], check=True)
        subprocess.run(['launchctl', 'bootout', target], capture_output=True)
        return
    if action == 'start':
        body = dict(Label=LABEL, ProgramArguments=[str(ROOT / '.venv-asr/bin/python'), str(Path(__file__).resolve()),
                    'run', '--manifest', str(Path(manifest).resolve())], WorkingDirectory=str(ROOT),
                    RunAtLoad=True, KeepAlive={'SuccessfulExit': False}, ThrottleInterval=60,
                    ProcessType='Background', Nice=10,
                    StandardOutPath=str(core.STATE / 'worker.log'), StandardErrorPath=str(core.STATE / 'worker.err'),
                    EnvironmentVariables=dict(PATH='/opt/homebrew/bin:/usr/bin:/bin', CHAT_ASR_LAUNCHD='1', HF_HUB_OFFLINE='1',
                       TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1'))
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(body))
        PLIST.chmod(0o600)
        subprocess.run(['launchctl', 'enable', target], check=True)
        exists = subprocess.run(['launchctl', 'print', target], capture_output=True).returncode == 0
        if not exists:
            subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(PLIST)], check=True)
        else:
            subprocess.run(['launchctl', 'kickstart', target], check=True)


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['create', 'init', 'resolve-formats', 'run', 'resume', 'status', 'verify', 'retry', 'start', 'disable'])
    p.add_argument('--manifest', type=Path, default=MANIFEST)
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--platform', choices=list(core.DATABASES))
    a = p.parse_args()
    if a.limit < 0:
        p.error('--limit must be nonnegative')
    core.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if a.action == 'create':
        result = create_manifest(a.manifest)
    elif a.action == 'init':
        result = initialise(a.manifest)
        result = dict(batch_id=result['batch_id'], counts={p: len(v) for p, v in result['items'].items()})
    elif a.action in ('run', 'resume'):
        result = run(a.manifest, a.limit, a.platform)
    elif a.action in ('start', 'disable'):
        manage(a.action, a.manifest)
        result = dict(action=a.action, label=LABEL)
    else:
        manifest, _ = core.load_manifest(a.manifest)
        if a.action == 'status':
            result = dict(batch_id=manifest['batch_id'], counts=batch_status(manifest))
            try:
                result['worker'] = json.loads((core.STATE / 'status.json').read_text())
                try:
                    os.kill(result['worker']['pid'], 0)
                    result['worker_alive'] = True
                except (OSError, KeyError):
                    result['worker_alive'] = False
            except (OSError, ValueError):
                result['worker'] = None
        elif a.action == 'resolve-formats':
            result = resolve_formats(manifest)
        elif a.action == 'verify':
            result = acceptance(manifest)
        else:
            with core.exclusive_worker() as connections:
                for c in connections.values():
                    my.execute(c, "UPDATE voice_transcripts t JOIN voice_asr_items i ON i.transcript_id=t.id SET t.status='pending',t.error_code=NULL WHERE i.batch_id=%s AND t.status='failed'", (manifest['batch_id'],))
                    c.commit()
                result = batch_status(manifest, connections)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == '__main__':
    def stop(signum, frame):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def expired(signum, frame):
        raise TimeoutError('local_inference_timeout')
    signal.signal(signal.SIGALRM, expired)
    try:
        main()
    except Exception as exc:
        code = str(exc) if isinstance(exc, core.AudioError) else type(exc).__name__
        state(status='failed', error_code=code)
        print(json.dumps(dict(status='failed', error_code=code)), file=sys.stderr)
        # Permanent model/verification failures must not poison more jobs on a restart loop.
        permanent = isinstance(exc, core.AudioError) and code in (
            'repeated_inference_failure', 'batch_configuration_changed', 'model_checksum_mismatch',
            'model_not_verified', 'model_revision_mismatch', 'final_verification_failed')
        raise SystemExit(0 if permanent and os.environ.get('CHAT_ASR_LAUNCHD') == '1' else 1)
