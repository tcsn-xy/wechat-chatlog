"""Bridge regression tests use synthetic identities and disposable local state."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'lib')]
from chatlog_core.bridge import Bridge, digest
from chatlog_core.bridge_store import Store
from chatlog_core.bridge_rules import question, wechat_mentions, wecom_segments
from chatlog_core.bridge_desktop import Desktop
from chatlog_core.bridge_web import server
from chatlog_core.common import QueryError
from mysql_store.wx_import import decode_message

BINDINGS = {'wechat': dict(account_key='wxid_self', self_id='wxid_self'),
            'wecom': dict(account_key='100', self_id='100')}
THREAD = '00000000-0000-7000-8000-000000000001'


def varint(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128); n >>= 7
    return bytes(out + bytes([n]))


def field(n, v):
    return varint(n * 8) + varint(v) if isinstance(v, int) else varint(n * 8 + 2) + varint(len(v)) + v


def segment(typ, value):
    payload = field(1, value)
    return field(1, field(1, typ) + field(2, payload))


def message(platform='wechat', room=False, sender=None, text='豆包豆包 问题', **overrides):
    row = dict(id=11, platform=platform, account_key=BINDINGS[platform]['account_key'], sender_id=sender or ('wxid_friend' if platform == 'wechat' else '200'),
               chat_ref=platform + ':chat:1', message_ref=platform + ':message:11', chat_id='room@chatroom' if room else 'wxid_friend',
               is_room=room, text=text, kind='text', ts=int(time.time()), server_id='123', source_dataset='fixture', source_db='message.db', source_table='Msg_fixture', local_id=11,
               raw_blob=segment(0, text.encode()) if platform == 'wecom' else text.encode(), raw_ready=True,
               mention_source='<msgsource><atuserlist>wxid_self</atuserlist></msgsource>' if room and platform == 'wechat' else None)
    row.update(overrides)
    return row


class FakeDesktop:
    def __init__(self):
        self.calls = []
        self.result = dict(status='desktop_queued', native_message_id='native-fixture')
    def running(self): return True
    def binding(self, thread, workspace):
        if thread != THREAD or workspace != '/fixture':
            raise QueryError('desktop_binding_mismatch', 'fixture mismatch')
    def submit(self, *args):
        self.binding(args[0], args[1]); self.calls.append(args); return self.result


class FakeFeed:
    def __init__(self): self.items = []; self.top = 10
    def highwater(self, p): return self.top
    def verify_identity(self, i): return i['contact_id'] in ('wxid_friend', '200')
    def verify_chat(self, record): return True
    def rows(self, p, after): return [r for r in self.items if r['platform'] == p and r['id'] > after]


class Rules(unittest.TestCase):
    def test_private_keyword_and_empty_question(self):
        self.assertEqual(question(message(), 'wxid_self'), '问题')
        self.assertEqual(question(message(text='请看这个问题，豆包豆包'), 'wxid_self'), '请看这个问题')
        for text in ('普通聊天', '豆包豆包', '豆包豆包： '):
            self.assertIsNone(question(message(text=text), 'wxid_self'))

    def test_wechat_group_requires_both_and_ignores_fake_xml_in_body(self):
        self.assertEqual(question(message(room=True, text='@本人\u2005豆包豆包 问题'), 'wxid_self'), '问题')
        self.assertIsNone(question(message(room=True, text='@本人\u2005问题'), 'wxid_self'))
        self.assertIsNone(question(message(room=True, mention_source=None), 'wxid_self'))
        self.assertIsNone(question(message(room=True, mention_source='<msgsource><atuserlist>wxid_other</atuserlist></msgsource>'), 'wxid_self'))
        self.assertIsNone(question(message(room=True, text='豆包豆包 <msgsource><atuserlist>wxid_self</atuserlist></msgsource>', mention_source=None), 'wxid_self'))

    def test_owner_does_not_need_to_mention_self(self):
        self.assertEqual(question(message(room=True, sender='wxid_self', mention_source=None), 'wxid_self'), '问题')

    def test_group_id_cannot_bypass_mentions_with_stale_room_flag(self):
        self.assertIsNone(question(message(room=True, is_room=False, mention_source=None), 'wxid_self'))

    def test_nontext_and_invalid_source_do_not_trigger(self):
        self.assertIsNone(question(message(kind='quote'), 'wxid_self'))
        self.assertIsNone(wechat_mentions('<msg><atuserlist>wxid_self</atuserlist></msg>'))
        self.assertIsNone(wechat_mentions('<broken'))

    def test_wecom_structured_mention_and_split_text(self):
        raw = segment(5, 100) + segment(0, '豆包豆包 '.encode()) + segment(0, 'Unicode 🦋 问题'.encode())
        self.assertEqual(question(message('wecom', room=True, raw_blob=raw), '100'), 'Unicode 🦋 问题')
        self.assertIsNone(question(message('wecom', room=True, raw_blob=segment(5, 300) + segment(0, '豆包豆包 问题'.encode())), '100'))
        self.assertIsNone(question(message('wecom', room=True, raw_blob=segment(0, '豆包豆包 @本人 问题'.encode())), '100'))

    def test_wecom_unknown_structure_and_quoted_metadata_are_ignored(self):
        self.assertEqual(wecom_segments(b'not protobuf'), (None, None))
        raw = segment(0, '豆包豆包 问题'.encode())
        row = message('wecom', room=True, raw_blob=raw, extra_blob=segment(5, 100))
        self.assertIsNone(question(row, '100'))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'state')
        self.desktop, self.feed = FakeDesktop(), FakeFeed()
        self.bridge = Bridge(self.store, self.feed, self.desktop, BINDINGS)
        self.config = dict(paused=False, principals=[dict(id='friend', name='Friend', enabled=True, identities_confirmed=True, workspace='/fixture', thread_id=THREAD,
            identities=[dict(platform='wechat', account_key='wxid_self', contact_id='wxid_friend', enabled=True), dict(platform='wecom', account_key='100', contact_id='200', enabled=True)])])
        self.config=self.bridge.save(copy.deepcopy(self.config))
    def tearDown(self): self.tmp.cleanup()
    def last(self):
        with self.store.db() as c: return dict(c.execute('SELECT * FROM deliveries ORDER BY created DESC LIMIT 1').fetchone())

    def test_all_platforms_and_chats_use_one_persons_thread(self):
        for platform in ('wechat', 'wecom'):
            for room in (False, True):
                raw = segment(5, 100) + segment(0, '豆包豆包 问题'.encode()) if platform == 'wecom' else None
                kwargs = dict(raw_blob=raw) if raw else {}
                self.assertEqual(self.bridge.deliver(message(platform, room, server_id=str(len(self.desktop.calls) + 1), **kwargs)), 'desktop_queued')
        self.assertEqual(len(self.desktop.calls), 4)
        self.assertEqual({r[0] for r in self.desktop.calls}, {THREAD})

    def test_duplicate_source_survives_restart_but_new_message_with_same_body_runs(self):
        row = message()
        self.assertEqual(self.bridge.deliver(row), 'desktop_queued')
        restarted = Bridge(Store(self.store.root), self.feed, self.desktop, BINDINGS)
        self.assertEqual(restarted.deliver(row), 'duplicate')
        self.assertEqual(restarted.deliver(dict(row, server_id='new-id', id=12)), 'desktop_queued')
        self.assertEqual(len(self.desktop.calls), 2)

    def test_unknown_is_not_resubmitted(self):
        self.desktop.result = dict(status='unknown')
        self.assertEqual(self.bridge.deliver(message()), 'unknown')
        self.assertEqual(self.bridge.deliver(message()), 'duplicate')
        self.assertEqual(len(self.desktop.calls), 1)

    def test_concurrent_observers_only_submit_once(self):
        from concurrent.futures import ThreadPoolExecutor
        def submit(_):
            return Bridge(Store(self.store.root), self.feed, self.desktop, BINDINGS).deliver(message())
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, range(12)))
        self.assertEqual(results.count('desktop_queued'), 1)
        self.assertEqual(results.count('duplicate'), 11)
        self.assertEqual(len(self.desktop.calls), 1)

    def test_unauthorized_account_sender_and_history(self):
        self.assertEqual(self.bridge.deliver(message(sender='wxid_stranger')), 'not_authorized')
        self.assertEqual(self.bridge.deliver(message(account_key='another')), 'account_mismatch')
        self.assertEqual(self.bridge.deliver(message(ts=1)), 'before_activation')
        self.assertEqual(self.desktop.calls, [])

    def test_pause_revoke_and_platform_disable(self):
        config = self.store.config(); config['principals'][0]['identities'][0]['enabled'] = False
        config=self.bridge.save(config)
        self.assertEqual(self.bridge.deliver(message()), 'not_authorized')
        config['paused'] = True; self.bridge.save(config)
        self.assertEqual(self.bridge.deliver(message('wecom')), 'not_authorized')

    def test_binding_mismatch_and_unconfirmed_merge_are_rejected(self):
        config = copy.deepcopy(self.config); config['principals'][0]['thread_id'] = 'another'
        with self.assertRaises(QueryError): self.bridge.save(config)
        config = copy.deepcopy(self.config); config['principals'][0]['identities_confirmed'] = False
        with self.assertRaises(QueryError): self.bridge.save(config)

    def test_duplicate_identities_rejected(self):
        config = copy.deepcopy(self.config); other = copy.deepcopy(config['principals'][0]); other['id'] = 'other'; config['principals'].append(other)
        with self.assertRaises(QueryError): self.bridge.save(config)

    def test_initial_and_resume_baseline(self):
        self.assertEqual(self.store.cursor('wechat'), 10)
        config = self.store.config(); config['paused'] = True; config=self.bridge.save(config)
        self.feed.top = 20; self.bridge.poll()
        self.assertEqual(self.store.cursor('wechat'), 20)
        config['paused'] = False; self.bridge.save(config)
        self.assertEqual(self.store.cursor('wechat'), 20)

    def test_wait_for_raw_import_before_advancing(self):
        self.feed.items = [message(raw_ready=False), message(id=12, server_id='second')]
        self.bridge.poll()
        self.assertEqual(self.store.cursor('wechat'), 10)
        self.assertEqual(self.desktop.calls, [])
        self.feed.items[0]['raw_ready'] = True; self.bridge.poll()
        self.assertEqual(self.store.cursor('wechat'), 12)
        self.assertEqual(len(self.desktop.calls), 2)

    def test_reply_is_bound_to_origin_and_request_id(self):
        self.bridge.deliver(message()); record = self.last(); calls = []
        def send(r, text): calls.append((r['chat_ref'], r['request_id'], text)); return dict(ok=True, data=dict(status='server_accepted'))
        with patch.dict(os.environ, CODEX_THREAD_ID=THREAD):
            for _ in range(2): self.bridge.reply(record['token'], '答案 🦋', send)
            with self.assertRaises(QueryError): self.bridge.reply(record['token'], '改变正文', send)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][0], 'wechat:chat:1')
        self.assertNotIn('答案', self.store.path.read_bytes().decode('latin1'))

    def test_reply_after_revoke_or_from_wrong_thread_is_rejected(self):
        self.bridge.deliver(message()); record = self.last()
        with patch.dict(os.environ, CODEX_THREAD_ID='another'):
            with self.assertRaises(QueryError): self.bridge.reply(record['token'], 'answer')
        config = self.store.config(); config['paused'] = True; config=self.bridge.save(config)
        with patch.dict(os.environ, CODEX_THREAD_ID=THREAD):
            with self.assertRaises(QueryError): self.bridge.reply(record['token'], 'answer')
        config['paused'] = False; self.bridge.save(config)
        with patch.dict(os.environ, CODEX_THREAD_ID=THREAD):
            with self.assertRaises(QueryError): self.bridge.reply(record['token'], 'old answer after resume')

    def test_recycled_chat_reference_is_rejected(self):
        self.bridge.deliver(message()); record = self.last()
        with patch.dict(os.environ, CODEX_THREAD_ID=THREAD), patch.object(self.feed, 'verify_chat', return_value=False):
            with self.assertRaises(QueryError) as e: self.bridge.reply(record['token'], 'answer')
        self.assertEqual(e.exception.detail['code'], 'bridge_target_changed')

    def test_own_reply_does_not_loop(self):
        config = copy.deepcopy(self.config)
        owner = copy.deepcopy(config['principals'][0]); owner['id'] = 'owner'; owner['name'] = 'Owner'; owner['identities'] = [dict(platform='wechat', account_key='wxid_self', contact_id='wxid_self', enabled=True)]
        config['principals'].append(owner); self.bridge.save(config)
        self.bridge.deliver(message()); record = self.last()
        with patch.dict(os.environ, CODEX_THREAD_ID=THREAD):
            self.bridge.reply(record['token'], '豆包豆包 答案', lambda *_: dict(ok=True))
        self.assertEqual(self.bridge.deliver(message(sender='wxid_self', text='豆包豆包 答案', server_id='reply')), 'own_reply')

    def test_media_reply_binds_payload_and_replays_without_resending(self):
        from unittest.mock import Mock
        from chatlog_core import sending
        from PIL import Image
        self.bridge.deliver(message());record=self.last()
        path=Path(self.tmp.name)/'figure.png';Image.new('RGB',(8,8),'green').save(path)
        attachments=[dict(type='image',path=str(path))]
        adapter=Mock();adapter.media_capabilities.return_value={'image':dict(supported=True,max_bytes=1024*1024)}
        adapter.health.return_value=dict(ready=True)
        adapter.send.return_value=dict(status='server_accepted')
        adapter.send_attachment.return_value=dict(status='server_accepted',resources_released=True)
        with patch.dict(os.environ,CODEX_THREAD_ID=THREAD),patch.object(sending,'STATE',Path(self.tmp.name)/'sending'),patch.object(sending,'load_settings',return_value=BINDINGS),patch.object(sending,'NativeAdapter',return_value=adapter):
            first=self.bridge.reply(record['token'],'豆包豆包 fixture answer',attachments=attachments)
            second=self.bridge.reply(record['token'],'豆包豆包 fixture answer',attachments=attachments)
            self.assertTrue(first['ok']);self.assertTrue(second['data']['replayed'])
            adapter.send_attachment.assert_called_once()
            adapter.send.assert_called_once()
            with self.assertRaises(QueryError):self.bridge.reply(record['token'],'changed',attachments=attachments)
            self.assertTrue(self.store.is_reply('wechat',BINDINGS['wechat']['account_key'],record['chat_ref'],digest('豆包豆包 fixture answer'),0))
            with patch.dict(os.environ,CODEX_THREAD_ID='wrong'):
                with self.assertRaises(QueryError):self.bridge.reply(record['token'],'豆包豆包 fixture answer',attachments=attachments)

    def test_media_reply_rechecks_revocation_between_items(self):
        from unittest.mock import Mock
        from chatlog_core import sending
        self.bridge.deliver(message());record=self.last()
        path=Path(self.tmp.name)/'fixture.txt';path.write_text('synthetic fixture')
        adapter=Mock();adapter.media_capabilities.return_value={'file':dict(supported=True,max_bytes=1024)}
        adapter.health.return_value=dict(ready=True)
        def send_text(*args):
            config=self.store.config();config['paused']=True;self.bridge.save(config)
            return dict(status='server_accepted')
        adapter.send.side_effect=send_text
        with patch.dict(os.environ,CODEX_THREAD_ID=THREAD),patch.object(sending,'STATE',Path(self.tmp.name)/'sending'),patch.object(sending,'load_settings',return_value=BINDINGS),patch.object(sending,'NativeAdapter',return_value=adapter):
            result=self.bridge.reply(record['token'],'fixture answer',attachments=[dict(type='file',path=str(path))])
            self.assertTrue(result['partial']);self.assertEqual(result['data']['status'],'partial')
            self.assertFalse(result['data']['items'][1]['attempted'])
            adapter.send_attachment.assert_not_called()

    def test_no_body_or_prompt_persisted(self):
        self.bridge.deliver(message(text='豆包豆包 BODY_ONLY_SECRET'))
        self.assertNotIn(b'BODY_ONLY_SECRET', self.store.path.read_bytes())
        self.assertIn('BODY_ONLY_SECRET', self.desktop.calls[0][2])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)


class DesktopTests(unittest.TestCase):
    def test_catalog_binding_and_queue_without_starting_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); c = sqlite3.connect(home / 'state_1.sqlite')
            c.execute('CREATE TABLE threads(id,title,cwd,updated_at,archived,agent_role)')
            c.execute('INSERT INTO threads VALUES (?,?,?,?,0,NULL)', (THREAD, 'Fixture', str(home), 1)); c.commit(); c.close()
            desktop = Desktop(home, cli=home / 'codex')
            with patch.object(desktop, 'running', return_value=True), patch('chatlog_core.bridge_desktop.subprocess.run') as run:
                run.return_value.stdout = 'Queued message 00000000-0000-7000-8000-000000000002 for thread ' + THREAD + '.\n'; run.return_value.returncode = 0
                self.assertEqual(desktop.submit(THREAD, str(home), 'hello')['status'], 'desktop_queued')
                self.assertEqual(run.call_args.args[0][1:4], ['queue', '--thread', THREAD])
                self.assertNotIn('exec', run.call_args.args[0])
            with self.assertRaises(QueryError): desktop.binding(THREAD, '/wrong')


class WebTests(unittest.TestCase):
    def test_admin_auth_origin_and_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(Store(Path(tmp) / 'state'), FakeFeed(), FakeDesktop(), BINDINGS)
            httpd = server(bridge, 0); worker = threading.Thread(target=httpd.serve_forever, daemon=True); worker.start()
            base = 'http://127.0.0.1:' + str(httpd.server_port)
            try:
                with urllib.request.urlopen(base + '/') as r:
                    self.assertIn(b'bridge_ui.js', r.read()); self.assertEqual(r.headers['Referrer-Policy'], 'no-referrer')
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(base + '/api/state')
                self.assertEqual(error.exception.code, 401)
                headers = {'Authorization': 'Bearer ' + bridge.store.admin_token()}
                with urllib.request.urlopen(urllib.request.Request(base + '/api/state', headers=headers)) as r: self.assertTrue(json.load(r)['config']['paused'])
                headers['Origin'] = 'https://evil.example'
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(urllib.request.Request(base + '/api/state', headers=headers))
                self.assertEqual(error.exception.code, 403)
            finally:
                httpd.shutdown(); httpd.server_close(); worker.join()


class SenderIdentityTests(unittest.TestCase):
    def test_single_chat_partner_not_system_and_group_system_stays_system(self):
        class Directory:
            def display(self, sender, room): return sender
        class Importer:
            self_wxid = 'wxid_self'
            dir = Directory()
            def _extract_links(self, *_): return []
            def _fmt(self, value): return str(value)
        row = (1, 1, 2, 100, 'hello', 123, 1, 0)
        result = decode_message(Importer(), row, True, 'wxid_friend', 1, 'message.db', 'Msg_fixture', {2: 'wxid_friend'})
        self.assertEqual(result[4], 'wxid_friend')
        result = decode_message(Importer(), row, True, 'fixture@chatroom', 1, 'message.db', 'Msg_fixture', {2: 'fixture@chatroom'})
        self.assertEqual(result[4], '系统')
        result = decode_message(Importer(), (1, 10000, 2, 100, '系统通知', 123, 1, 0), True, 'wxid_friend', 1, 'message.db', 'Msg_fixture', {2: 'wxid_friend'})
        self.assertEqual(result[4], '系统')
        result = decode_message(Importer(), row + ('<msgsource><atuserlist>wxid_self</atuserlist></msgsource>', 0), True, 'wxid_friend', 1, 'message.db', 'Msg_fixture', {2: 'wxid_friend'})
        self.assertEqual(json.loads(result[1][-1])['wechat_source'], '<msgsource><atuserlist>wxid_self</atuserlist></msgsource>')


if __name__ == '__main__': unittest.main()
