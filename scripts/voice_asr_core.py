"""Local, frozen-batch voice transcription. Never modifies messages or media rows."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import wave

from mysql_store import conn as my, files

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.runtime/voice-asr'
MODEL = ROOT / 'models/Qwen3-ASR-0.6B-8bit'
DATABASES = {'wechat': 'wx_chat', 'wecom': 'wecom_chat'}
CONFIG = dict(model='mlx-community/Qwen3-ASR-0.6B-8bit',
              revision='89e96d92ba34aca20b3e29fb10cc284097d1219f',
              engine='mlx-audio-0.5.9', sample_rate=16000,
              language='auto', temperature=0, max_tokens=1024,
              preparation_version='wav16k-mono-v1')
CONFIG_ID = hashlib.sha256(json.dumps(CONFIG, sort_keys=True).encode()).hexdigest()
TERMINAL = ('complete', 'needs_review', 'silence', 'failed', 'pending_format')


class AudioError(Exception):
    """Code only; no private speech, command output or source paths in logs."""


def prepare(c):
    # Dedicated additive migration: no legacy schema/media merging.
    statements = ["""CREATE TABLE IF NOT EXISTS voice_asr_batches (
      batch_id VARCHAR(64) PRIMARY KEY, platform VARCHAR(16) NOT NULL,
      manifest_sha256 CHAR(64) NOT NULL, config_id CHAR(64) NOT NULL,
      config_json JSON NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      finished_at DATETIME, item_count INT NOT NULL,
      status VARCHAR(24) NOT NULL DEFAULT 'pending') ENGINE=InnoDB""",
      """CREATE TABLE IF NOT EXISTS voice_transcripts (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, platform VARCHAR(16) NOT NULL,
      account_key VARCHAR(191) NOT NULL, input_sha256 CHAR(64) NOT NULL,
      config_id CHAR(64) NOT NULL, model_version VARCHAR(255) NOT NULL,
      status VARCHAR(24) NOT NULL DEFAULT 'pending', text MEDIUMTEXT,
      segments JSON, language VARCHAR(64), timing_kind VARCHAR(32),
      confidence_supported BOOLEAN NOT NULL DEFAULT FALSE, review_reason VARCHAR(64),
      normalized_sha256 CHAR(64), normalized_path VARCHAR(1024), duration_ms INT,
      metrics JSON, attempts INT NOT NULL DEFAULT 0, error_code VARCHAR(64),
      started_at DATETIME, completed_at DATETIME,
      UNIQUE KEY uk_voice_input(platform,account_key,input_sha256,config_id),
      KEY ix_voice_queue(status,id)) ENGINE=InnoDB""",
      """CREATE TABLE IF NOT EXISTS voice_asr_items (
      batch_id VARCHAR(64) NOT NULL, media_id BIGINT UNSIGNED NOT NULL,
      platform VARCHAR(16) NOT NULL, account_key VARCHAR(191) NOT NULL,
      message_pk BIGINT UNSIGNED, transcript_id BIGINT UNSIGNED NOT NULL,
      original_sha256 CHAR(64), decoded_sha256 CHAR(64), snapshot JSON NOT NULL,
      PRIMARY KEY(batch_id,media_id), KEY ix_voice_media(media_id),
      KEY ix_voice_transcript(transcript_id), KEY ix_voice_message(message_pk)) ENGINE=InnoDB"""
    ]
    for sql in statements:
        my.execute(c, sql)
    c.commit()


def load_manifest(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if not isinstance(data.get('batch_id'), str) or len(data['batch_id']) > 64:
        raise AudioError('invalid_manifest')
    if set(data.get('items', {})) - set(DATABASES):
        raise AudioError('invalid_platform')
    for platform, rows in data['items'].items():
        if len({r['id'] for r in rows}) != len(rows):
            raise AudioError('duplicate_media_id')
        for r in rows:
            if r['platform'] != platform or not r['account_key']:
                raise AudioError('invalid_account')
            for field in ('file_sha256', 'decoded_sha256'):
                value = r.get(field)
                if value and (len(value) != 64 or any(ch not in '0123456789abcdef' for ch in value)):
                    raise AudioError('invalid_hash')
    return data, hashlib.sha256(raw).hexdigest()


def seed_batch(c, manifest, digest, platform):
    batch = manifest['batch_id']
    old = my.query(c, 'SELECT manifest_sha256,config_id FROM voice_asr_batches WHERE batch_id=%s', (batch,))
    if old:
        if old[0] != dict(manifest_sha256=digest, config_id=CONFIG_ID):
            raise AudioError('batch_configuration_changed')
        return
    try:
        my.execute(c, 'INSERT INTO voice_asr_batches(batch_id,platform,manifest_sha256,config_id,config_json,item_count) VALUES(%s,%s,%s,%s,%s,%s)',
                   (batch, platform, digest, CONFIG_ID, json.dumps(CONFIG), len(manifest['items'][platform])))
        for r in manifest['items'][platform]:
            sha = r.get('decoded_sha256') or r.get('file_sha256')
            # Missing hashes must not share a fabricated identity with other files.
            if not sha:
                sha = hashlib.sha256(('unavailable-media:' + str(r['id'])).encode()).hexdigest()
            readable = bool(r.get('decoded_path') and r.get('decoded_sha256')) or (
                r.get('file_path') and r.get('file_sha256') and r.get('ext') in ('wav', 'mp3', 'amr', 'm4a', 'ogg'))
            status = 'pending' if readable else 'pending_format'
            my.execute(c, 'INSERT IGNORE INTO voice_transcripts(platform,account_key,input_sha256,config_id,model_version,status,error_code) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                       (platform, r['account_key'], sha, CONFIG_ID, CONFIG['model'] + '@' + CONFIG['revision'], status,
                        None if readable else 'unverified_audio_format'))
            pk = my.scalar(c, 'SELECT id FROM voice_transcripts WHERE platform=%s AND account_key=%s AND input_sha256=%s AND config_id=%s',
                           (platform, r['account_key'], sha, CONFIG_ID))
            my.execute(c, 'INSERT INTO voice_asr_items(batch_id,media_id,platform,account_key,message_pk,transcript_id,original_sha256,decoded_sha256,snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                       (batch, r['id'], platform, r['account_key'], r.get('message_pk'), pk,
                        r.get('file_sha256'), r.get('decoded_sha256'), json.dumps(r, default=str)))
        c.commit()
    except BaseException:
        c.rollback()
        raise


def summary(c, batch):
    return dict(items=my.query(c, 'SELECT t.status,COUNT(*) n FROM voice_asr_items i JOIN voice_transcripts t ON t.id=i.transcript_id WHERE i.batch_id=%s GROUP BY t.status', (batch,)),
                unique_inputs=my.query(c, 'SELECT t.status,COUNT(DISTINCT t.id) n FROM voice_asr_items i JOIN voice_transcripts t ON t.id=i.transcript_id WHERE i.batch_id=%s GROUP BY t.status', (batch,)),
                errors=my.query(c, 'SELECT t.error_code,COUNT(*) n FROM voice_asr_items i JOIN voice_transcripts t ON t.id=i.transcript_id WHERE i.batch_id=%s AND t.error_code IS NOT NULL GROUP BY t.error_code', (batch,)))


def recover(c, batch):
    # Called only with the worker's OS and MySQL advisory locks held.
    my.execute(c, "UPDATE voice_transcripts t JOIN voice_asr_items i ON i.transcript_id=t.id SET t.status='pending',t.error_code='interrupted_retry' WHERE i.batch_id=%s AND t.status='processing'", (batch,))
    c.commit()


def claim(c, batch):
    try:
        rows = my.query(c, "SELECT t.id FROM voice_transcripts t WHERE t.status='pending' AND EXISTS(SELECT 1 FROM voice_asr_items i WHERE i.transcript_id=t.id AND i.batch_id=%s) ORDER BY t.id LIMIT 1 FOR UPDATE", (batch,))
        if not rows:
            c.commit()
            return None
        pk = rows[0]['id']
        my.execute(c, "UPDATE voice_transcripts SET status='processing',started_at=NOW(),attempts=attempts+1,error_code=NULL WHERE id=%s", (pk,))
        rows = my.query(c, 'SELECT snapshot FROM voice_asr_items WHERE batch_id=%s AND transcript_id=%s ORDER BY media_id', (batch, pk))
        c.commit()
        return pk, [json.loads(r['snapshot']) for r in rows]
    except BaseException:
        c.rollback()
        raise


def verified_source(r, base=None):
    paths = []
    for column, digest, old_hash in [('file_path', 'file_sha256', 'sha1'),
                                    ('decoded_path', 'decoded_sha256', 'decoded_sha1')]:
        if not r.get(column):
            continue
        try:
            p = files.resolve(r[column], base)
            actual = files.hashes(p)
        except FileNotFoundError:
            raise AudioError('file_missing') from None
        except ValueError:
            raise AudioError('invalid_path') from None
        if not r.get(digest) or actual['sha256'] != r[digest]:
            raise AudioError('file_checksum_mismatch')
        if r.get(old_hash) and actual['sha1'] != r[old_hash]:
            raise AudioError('legacy_checksum_mismatch')
        if column == 'file_path' and r.get('bytes') is not None and actual['bytes'] != int(r['bytes']):
            raise AudioError('file_length_mismatch')
        paths.append((column, p))
    if not paths:
        raise AudioError('file_missing')
    source = next((p for col, p in paths if col == 'decoded_path'), paths[0][1])
    if not r.get('decoded_path') and r.get('ext') == 'pcm':
        raise AudioError('unverified_audio_format')
    return source


def prepare_audio(r, base=None):
    try:
        source = verified_source(r, base)
    except AudioError as exc:
        if str(exc) != 'unverified_audio_format':
            raise
        try:
            evidence = json.loads((STATE / 'format-evidence.json').read_text())
            entry = evidence[f"{r['platform']}:{r['id']}"]
            if entry['account_key'] != r['account_key'] or entry['pcm_sha256'] != r['file_sha256'] or entry['verification'] != 'silk_decode_exact_pcm_match':
                raise AudioError('invalid_format_evidence')
            source = files.resolve(entry['wav_path'], base)
            if files.hashes(source)['sha256'] != entry['wav_sha256']:
                raise AudioError('format_evidence_checksum_mismatch')
            witness = files.resolve(entry['silk_path'], base)
            if files.hashes(witness)['sha256'] != entry['silk_sha256']:
                raise AudioError('format_evidence_checksum_mismatch')
        except (OSError, ValueError, KeyError):
            raise AudioError('unverified_audio_format') from None
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='input-', dir=STATE) as temp:
        target = Path(temp) / 'recognition.wav'
        binary = os.environ.get('FFMPEG') or shutil.which('ffmpeg') or 'ffmpeg'
        try:
            result = subprocess.run([binary, '-nostdin', '-v', 'error', '-i', str(source),
                                     '-map', '0:a:0', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(target)],
                                    capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise AudioError('audio_decode_timeout') from None
        except FileNotFoundError:
            raise AudioError('ffmpeg_missing') from None
        if result.returncode or not target.exists():
            raise AudioError('audio_decode_failed')
        with wave.open(str(target)) as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1 or wav.getframerate() != 16000:
                raise AudioError('invalid_recognition_wav')
            samples = wav.readframes(wav.getnframes())
            duration = wav.getnframes() / 16000
        if not samples or duration <= 0 or duration > 3600:
            raise AudioError('invalid_audio_duration')
        import array
        a = array.array('h', samples)
        if __import__('sys').byteorder != 'little':
            a.byteswap()
        rms = math.sqrt(sum(v*v for v in a) / len(a)) / 32768
        peak = max(abs(v) for v in a) / 32768
        # Only near-digital silence; quiet speech must still reach the recognizer.
        silence = rms < 0.00001 and peak < 0.00004
        saved = files.publish([target.read_bytes()], r['platform'], r['account_key'], 'wav', base)
    return dict(path=files.resolve(saved['path'], base), normalized_path=saved['path'],
                normalized_sha256=saved['sha256'], duration_ms=round(duration * 1000),
                silence=silence, rms=rms, peak=peak)


def parse_result(result, duration_ms):
    text = getattr(result, 'text', None)
    if not isinstance(text, str):
        raise AudioError('invalid_model_result')
    text = text.strip()
    if len(text) > 16000:
        raise AudioError('excessive_model_output')
    segments = getattr(result, 'segments', None) or []
    valid = []
    for seg in segments:
        if not isinstance(seg, dict):
            raise AudioError('invalid_model_segments')
        start, end = seg.get('start'), seg.get('end')
        if start is not None and end is not None:
            if not (isinstance(start, (float, int)) and isinstance(end, (float, int)) and
                    math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= duration_ms / 1000 + 1):
                raise AudioError('invalid_model_segments')
        valid.append(dict(text=seg.get('text', ''), start=start, end=end, language=seg.get('language')))
    reason = None
    if not text:
        reason = 'empty_result'
    elif len(text) / max(duration_ms / 1000, 1) > 20:
        reason = 'implausible_text_rate'
    elif len(text) > 20 and len(set(text)) < 5:
        reason = 'repetitive_output'
    elif getattr(result, 'generation_tokens', 0) >= CONFIG['max_tokens']:
        reason = 'token_limit'
    metrics = {key: getattr(result, key, None) for key in
               ('total_time', 'generation_tokens', 'prompt_tokens', 'generation_tps')}
    language = getattr(result, 'language', None)
    if isinstance(language, list):
        language = ','.join(dict.fromkeys(v for v in language if isinstance(v, str) and v))
    if language is not None and not isinstance(language, str):
        raise AudioError('invalid_model_language')
    return dict(text=text, segments=valid, status='needs_review' if reason else 'complete',
                review_reason=reason, language=(language or 'auto')[:64],
                timing_kind='chunk', metrics=metrics)


def finish(c, pk, audio, result):
    my.execute(c, 'UPDATE voice_transcripts SET status=%s,text=%s,segments=%s,language=%s,timing_kind=%s,review_reason=%s,normalized_sha256=%s,normalized_path=%s,duration_ms=%s,metrics=%s,error_code=NULL,completed_at=NOW() WHERE id=%s AND status=\'processing\'',
               (result['status'], result.get('text'), json.dumps(result.get('segments', [])), result.get('language'),
                result.get('timing_kind'), result.get('review_reason'), audio['normalized_sha256'],
                audio['normalized_path'], audio['duration_ms'], json.dumps(result.get('metrics', {})), pk))
    c.commit()


def fail(c, pk, code):
    my.execute(c, "UPDATE voice_transcripts SET status=%s,error_code=%s,completed_at=NOW() WHERE id=%s AND status='processing'",
               ('pending_format' if code == 'unverified_audio_format' else 'failed', code, pk))
    c.commit()


def verify_batch(manifest, base=None):
    out = dict(items=0, unique_files=0, bytes=0, failed=0, failures=[])
    seen = {}
    for platform, rows in manifest['items'].items():
        for r in rows:
            out['items'] += 1
            try:
                for pathcol, hashcol, sha1col in [('file_path', 'file_sha256', 'sha1'), ('decoded_path', 'decoded_sha256', 'decoded_sha1')]:
                    if not r.get(pathcol):
                        continue
                    p = files.resolve(r[pathcol], base)
                    actual = seen.get(p)
                    if actual is None:
                        actual = files.hashes(p)
                        seen[p] = actual
                        out['bytes'] += actual['bytes']
                        out['unique_files'] += 1
                    if actual['sha256'] != r.get(hashcol) or (r.get(sha1col) and actual['sha1'] != r[sha1col]):
                        raise AudioError('file_checksum_mismatch')
                    if pathcol == 'file_path' and r.get('bytes') is not None and actual['bytes'] != int(r['bytes']):
                        raise AudioError('file_length_mismatch')
            except (OSError, ValueError, AudioError) as exc:
                out['failed'] += 1
                out['failures'].append(dict(platform=platform, media_id=r['id'],
                                            code=str(exc) if isinstance(exc, AudioError) else type(exc).__name__))
    return out


def verify_model(path=MODEL):
    try:
        metadata = json.loads((path / 'verified-model.json').read_text())
    except (OSError, ValueError):
        raise AudioError('model_not_verified') from None
    if metadata.get('repo') != CONFIG['model'] or metadata.get('revision') != CONFIG['revision']:
        raise AudioError('model_revision_mismatch')
    for entry in metadata['files']:
        p = path / entry['name']
        if not p.is_relative_to(path) or not p.is_file() or files.hashes(p)['sha256'] != entry['sha256']:
            raise AudioError('model_checksum_mismatch')


@contextlib.contextmanager
def exclusive_worker(databases=DATABASES):
    import fcntl
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AudioError('worker_already_running') from None
        connections = {}
        try:
            for platform, db in databases.items():
                c = my.connect(db)
                connections[platform] = c
                if my.scalar(c, 'SELECT GET_LOCK(%s,0)', ('chat-asr:' + db,)) != 1:
                    raise AudioError('worker_already_running')
                c.commit()
            yield connections
        finally:
            for c in connections.values():
                c.close()
