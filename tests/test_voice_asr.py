"""Synthetic audio and disposable repairtest_* databases only."""
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import wave
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'lib')]
from mysql_store import conn as my, schema, files
import voice_asr_core as asr

FFMPEG = os.environ.get('FFMPEG') or shutil.which('ffmpeg')
REQUIRES_FFMPEG = unittest.skipUnless(FFMPEG, 'ffmpeg is required for audio conversion')


def wav_bytes(silent=True):
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes((b'\0\0' if silent else b'\x00\x10') * 1600)
    return out.getvalue()


class AudioTests(unittest.TestCase):
    def test_invalid_empty_and_review_results(self):
        self.assertEqual(asr.parse_result(SimpleNamespace(text='中文 English', segments=[]), 3000)['status'], 'complete')
        languages = asr.parse_result(SimpleNamespace(text='中文 English', segments=[],language=['Chinese','English','Chinese']), 3000)
        self.assertEqual(languages['language'], 'Chinese,English')
        empty = asr.parse_result(SimpleNamespace(text='', segments=[]), 1000)
        self.assertEqual(empty['status'], 'needs_review')
        self.assertEqual(empty['review_reason'], 'empty_result')
        repeated = asr.parse_result(SimpleNamespace(text='啊' * 30, segments=[]), 5000)
        self.assertEqual(repeated['review_reason'], 'repetitive_output')
        for result in (SimpleNamespace(text=None), SimpleNamespace(text='x', segments=[dict(start=4,end=1)])):
            with self.assertRaises(asr.AudioError):
                asr.parse_result(result, 2000)

    @REQUIRES_FFMPEG
    def test_verified_audio_and_silence_preserve_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = files.publish([wav_bytes()], 'wechat', 'synthetic', 'wav', tmp)
            row = dict(platform='wechat', account_key='synthetic', file_path=entry['path'],
                       file_sha256=entry['sha256'], bytes=entry['bytes'], sha1=entry['sha1'], ext='wav')
            before = files.resolve(entry['path'], tmp).read_bytes()
            prepared = asr.prepare_audio(row, tmp)
            self.assertTrue(prepared['silence'])
            self.assertEqual(prepared['duration_ms'], 100)
            self.assertEqual(files.resolve(entry['path'], tmp).read_bytes(), before)
            self.assertTrue(prepared['path'].is_file())
            row['file_sha256'] = '0' * 64
            with self.assertRaisesRegex(asr.AudioError, 'checksum'):
                asr.prepare_audio(row, tmp)

    def test_raw_pcm_never_guesses_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = files.publish([b'\0\0'*100], 'wecom', 'fixture', 'pcm', tmp)
            row = dict(platform='wecom', account_key='fixture', file_path=entry['path'],
                       file_sha256=entry['sha256'], ext='pcm')
            with self.assertRaisesRegex(asr.AudioError, 'unverified_audio_format'):
                asr.prepare_audio(row, tmp)

    @REQUIRES_FFMPEG
    def test_missing_corrupted_and_disk_full_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = files.publish([wav_bytes(False)], 'wechat', 'fixture', 'wav', tmp)
            row = dict(platform='wechat', account_key='fixture', file_path=entry['path'],
                       file_sha256=entry['sha256'], ext='wav')
            with patch('voice_asr_core.files.publish', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    asr.prepare_audio(row, tmp)
            self.assertEqual(files.hashes(files.resolve(entry['path'], tmp))['sha256'], entry['sha256'])
            files.resolve(entry['path'], tmp).unlink()
            with self.assertRaisesRegex(asr.AudioError, 'file_missing'):
                asr.prepare_audio(row, tmp)

    def test_local_decode_timeout_keeps_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = files.publish([wav_bytes(False)], 'wechat', 'fixture', 'wav', tmp)
            row = dict(platform='wechat',account_key='fixture',file_path=entry['path'],file_sha256=entry['sha256'],ext='wav')
            with patch('voice_asr_core.subprocess.run',side_effect=subprocess.TimeoutExpired('ffmpeg',90)):
                with self.assertRaisesRegex(asr.AudioError,'audio_decode_timeout'):
                    asr.prepare_audio(row,tmp)
            self.assertEqual(files.hashes(files.resolve(entry['path'],tmp))['sha256'],entry['sha256'])

    def test_missing_ffmpeg_is_reported_not_leaked(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = files.publish([wav_bytes(False)], 'wechat', 'fixture', 'wav', tmp)
            row = dict(platform='wechat', account_key='fixture', file_path=entry['path'],
                       file_sha256=entry['sha256'], ext='wav')
            with patch('voice_asr_core.subprocess.run', side_effect=FileNotFoundError('ffmpeg')):
                with self.assertRaisesRegex(asr.AudioError, 'ffmpeg_missing'):
                    asr.prepare_audio(row, tmp)
            self.assertEqual(files.hashes(files.resolve(entry['path'], tmp))['sha256'], entry['sha256'])

    @REQUIRES_FFMPEG
    def test_pcm_evidence_is_hash_and_account_scoped(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(asr,'STATE',Path(tmp)/'state'):
            asr.STATE.mkdir()
            pcm = b'\0\0' * 800
            original = files.publish([pcm],'wecom','fixture','pcm',tmp)
            witness = files.publish([b'synthetic-silk-proof'],'wecom','fixture','silk',tmp)
            out=io.BytesIO()
            with wave.open(out,'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000);wav.writeframes(pcm)
            preview = files.publish([out.getvalue()],'wecom','fixture','wav',tmp)
            row=dict(id=1,platform='wecom',account_key='fixture',ext='pcm',file_path=original['path'],file_sha256=original['sha256'])
            evidence={'wecom:1':dict(account_key='fixture',pcm_sha256=original['sha256'],verification='silk_decode_exact_pcm_match',
                wav_path=preview['path'],wav_sha256=preview['sha256'],silk_path=witness['path'],silk_sha256=witness['sha256'])}
            path=asr.STATE/'format-evidence.json';path.write_text(json.dumps(evidence))
            self.assertEqual(asr.prepare_audio(row,tmp)['duration_ms'],100)
            evidence['wecom:1']['account_key']='other'
            path.write_text(json.dumps(evidence))
            with self.assertRaisesRegex(asr.AudioError,'invalid_format_evidence'):
                asr.prepare_audio(row,tmp)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db = 'repairtest_asr_' + uuid.uuid4().hex[:12]
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, CHAT_MEDIA_ROOT=self.temp.name)
        self.env.start()
        self.state_patch = patch.object(asr, 'STATE', Path(self.temp.name) / 'state')
        self.state_patch.start()
        with my.connect() as c:
            schema.create_all(c, self.db)
        self.c = my.connect(self.db)
        files.prepare(self.c)
        asr.prepare(self.c)
        entry = files.publish([wav_bytes(False)], 'wechat', 'fixture_a', 'wav', self.temp.name)
        self.rows = []
        for pk, account in [(1, 'fixture_a'), (2, 'fixture_a'), (3, 'fixture_b')]:
            my.execute(self.c, 'INSERT INTO messages(id,platform,account_key,chat_pk,chat_id,local_id,local_type,kind,ts,text,source_dataset,source_db,source_table) VALUES(%s,%s,%s,1,%s,%s,34,%s,1,%s,%s,%s,%s)',
                       (pk, 'wechat', account, 'fixture_room', pk, 'voice', '[语音] synthetic', 'synthetic', 'message_0.db', 'Msg_synthetic'))
            other = entry if account == 'fixture_a' else files.publish([wav_bytes(False)], 'wechat', account, 'wav', self.temp.name)
            row = dict(id=pk, platform='wechat', account_key=account, message_pk=pk,
                       msg_kind='voice', ext='wav', file_path=other['path'], file_sha256=other['sha256'],
                       bytes=other['bytes'], sha1=other['sha1'], decoded_path=None, decoded_sha256=None)
            my.execute(self.c, 'INSERT INTO media_items(id,platform,account_key,message_pk,msg_kind,ext,file_path,file_sha256,bytes,sha1) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                       tuple(row[k] for k in ('id','platform','account_key','message_pk','msg_kind','ext','file_path','file_sha256','bytes','sha1')))
            self.rows.append(row)
        self.c.commit()
        self.manifest = dict(batch_id='synthetic-batch', items={'wechat':self.rows})

    def tearDown(self):
        self.c.close()
        with my.connect() as c:
            my.execute(c, 'DROP DATABASE `' + self.db + '`')
        self.env.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    def seed(self):
        asr.seed_batch(self.c, self.manifest, 'a'*64, 'wechat')

    def test_frozen_batch_dedup_isolation_and_idempotence(self):
        self.seed()
        self.seed()
        self.assertEqual(my.scalar(self.c, 'SELECT COUNT(*) FROM voice_asr_items'), 3)
        self.assertEqual(my.scalar(self.c, 'SELECT COUNT(*) FROM voice_transcripts'), 2)
        my.execute(self.c, "INSERT INTO media_items(platform,account_key,msg_kind,file_sha256) VALUES('wechat','fixture_a','voice',%s)", ('b'*64,))
        self.c.commit()
        self.seed()
        self.assertEqual(my.scalar(self.c, 'SELECT COUNT(*) FROM voice_asr_items'), 3)
        with self.assertRaisesRegex(asr.AudioError, 'configuration_changed'):
            asr.seed_batch(self.c, self.manifest, 'c'*64, 'wechat')

    def test_atomic_seed_rollback(self):
        execute = my.execute
        def interrupt(c, sql, args=()):
            if sql.startswith('INSERT INTO voice_asr_items'):
                raise RuntimeError('synthetic interruption')
            return execute(c, sql, args)
        with patch('voice_asr_core.my.execute', side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                self.seed()
        self.assertEqual(my.scalar(self.c, 'SELECT COUNT(*) FROM voice_asr_batches'), 0)
        self.assertEqual(my.scalar(self.c, 'SELECT COUNT(*) FROM voice_transcripts'), 0)
        self.seed()

    def test_interrupted_processing_reclaims_and_reuses(self):
        self.seed()
        pk, snapshots = asr.claim(self.c, self.manifest['batch_id'])
        self.assertEqual(len(snapshots), 2)
        asr.recover(self.c, self.manifest['batch_id'])
        again, _ = asr.claim(self.c, self.manifest['batch_id'])
        self.assertEqual(again, pk)
        audio = dict(normalized_sha256='b'*64, normalized_path='synthetic.wav', duration_ms=100)
        result = dict(status='complete', text='合成中文 English %_!', segments=[], language='auto')
        asr.finish(self.c, pk, audio, result)
        self.assertEqual(my.scalar(self.c, 'SELECT COUNT(*) FROM voice_asr_items i JOIN voice_transcripts t ON i.transcript_id=t.id WHERE t.text=%s', (result['text'],)), 2)
        self.assertEqual(my.scalar(self.c, 'SELECT COUNT(*) FROM messages WHERE text=%s', ('[语音] synthetic',)), 3)
        self.assertEqual(my.scalar(self.c, 'SELECT COUNT(*) FROM media_items'), 3)
        counts = {r['status']:r['n'] for r in asr.summary(self.c,self.manifest['batch_id'])['items']}
        self.assertEqual(counts['complete'], 2)
        self.assertEqual(counts['pending'], 1)

    def test_finish_failure_does_not_commit_output(self):
        self.seed()
        pk, _ = asr.claim(self.c, self.manifest['batch_id'])
        audio = dict(normalized_sha256='b'*64, normalized_path='synthetic.wav', duration_ms=100)
        result = dict(status='complete', text='合成', language='auto')
        with patch.object(self.c, 'commit', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                asr.finish(self.c, pk, audio, result)
        self.c.rollback()
        row = my.query(self.c, 'SELECT status,text FROM voice_transcripts WHERE id=%s', (pk,))[0]
        self.assertEqual(row, dict(status='processing', text=None))
        asr.recover(self.c, self.manifest['batch_id'])
        self.assertEqual(my.scalar(self.c, 'SELECT status FROM voice_transcripts WHERE id=%s', (pk,)), 'pending')

    def test_file_verification_all_frozen_associations(self):
        self.seed()
        out = asr.verify_batch(self.manifest, self.temp.name)
        self.assertEqual(out['failed'], 0)
        self.assertEqual(out['unique_files'], 2)
        files.resolve(self.rows[0]['file_path'], self.temp.name).write_bytes(b'corrupted')
        self.assertEqual(asr.verify_batch(self.manifest, self.temp.name)['failed'], 2)

    def test_overlapping_workers_are_rejected(self):
        with asr.exclusive_worker({'wechat':self.db}):
            with self.assertRaisesRegex(asr.AudioError,'already_running'):
                with asr.exclusive_worker({'wechat':self.db}):
                    self.fail('overlapping worker acquired lock')

    def test_database_failure_leaves_queue_retryable(self):
        self.seed()
        with patch('voice_asr_core.my.query',side_effect=my.pymysql.OperationalError(2006,'synthetic disconnect')):
            with self.assertRaises(my.pymysql.OperationalError):
                asr.claim(self.c,self.manifest['batch_id'])
        self.assertEqual(my.scalar(self.c,"SELECT COUNT(*) FROM voice_transcripts WHERE status='pending'"),2)
        self.assertIsNotNone(asr.claim(self.c,self.manifest['batch_id']))


if __name__ == '__main__':
    unittest.main()
