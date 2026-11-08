"""Synthetic sender tests. No real clients, chats or credentials are accessed."""
import concurrent.futures
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from chatlog_core import sending
from chatlog_core.cli import execute, parser
from chatlog_core.common import QueryError
from chatlog_core.mcp_server import DEFINITIONS, TOOLS, invoke


class SendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.args = parser().parse_args(['send', '--chat', 'wechat:chat:7', '--text', '合成测试🌱\n第二行', '--request-id', 'synthetic-1'])
        self.chat = dict(platform='wechat', account_key='synthetic-account', chat_ref='wechat:chat:7', chat_id='synthetic-user')
        self.settings = {'wechat': dict(account_key='synthetic-account', self_id='synthetic-account')}
        self.adapter = Mock()
        self.adapter.health.return_value = dict(ready=True, status='ready_for_test')
        self.adapter.send.return_value = dict(status='server_accepted', task_id=123)
        self.factory = Mock(return_value=self.adapter)
        self.resolve = Mock(side_effect=lambda: dict(self.chat))

    def send(self):
        return sending.send(self.args, self.resolve, settings=self.settings, state=self.state, adapter_factory=self.factory)

    def test_exact_target_and_unicode(self):
        result = self.send()
        self.assertEqual(result['status'], 'server_accepted')
        self.adapter.send.assert_called_once_with('synthetic-user', self.args.text, 'synthetic-1')
        self.assertNotIn('text', result)
        self.assertEqual(sending.send_status('synthetic-1', state=self.state), result)
        self.assertEqual((self.state / 'receipts.sqlite3').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('合成', (self.state / 'receipts.sqlite3').read_bytes().decode(errors='ignore'))

    def test_replay_without_client_config_or_database(self):
        self.send()
        self.resolve.side_effect = AssertionError('must not read chats')
        self.factory.side_effect = AssertionError('must not access backend')
        with patch.object(sending, 'load_settings', side_effect=AssertionError('must not load settings')):
            result = self.send()
        self.assertTrue(result['replayed'])
        self.adapter.send.assert_called_once()

    def test_conflict_never_dispatches(self):
        self.send()
        for key, value in [('text', 'changed'), ('chat', 'wecom:chat:7')]:
            original = getattr(self.args, key)
            setattr(self.args, key, value)
            with self.assertRaises(QueryError) as error:
                self.send()
            self.assertEqual(error.exception.detail['code'], 'request_id_content_conflict')
            setattr(self.args, key, original)
        self.adapter.send.assert_called_once()

    def test_concurrent_replay_only_dispatches_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.send(), range(4)))
        self.adapter.send.assert_called_once()
        self.assertEqual(sum(r.get('replayed', False) for r in results), 3)

    def test_unknown_timeout_and_bad_response_are_never_retried(self):
        for answer in [dict(status='unknown'), dict(status='submitted_unconfirmed'), dict(status='not_sent'), dict(status='invalid')]:
            self.args.request_id += 'x'
            self.adapter.send.return_value = answer
            first = self.send()
            self.assertEqual(self.send()['status'], first['status'])
        self.assertEqual(self.adapter.send.call_count, 4)
        self.args.request_id += 'x'
        self.adapter.send.side_effect = TimeoutError('sensitive body or credentials')
        self.assertEqual(self.send()['status'], 'unknown')
        replay = self.send()
        self.assertTrue(replay['replayed'])
        self.assertNotIn('sensitive', json.dumps(replay))

    def test_reserved_receipt_survives_dispatch_crash(self):
        self.adapter.send.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.send()
        self.assertEqual(sending.send_status('synthetic-1', state=self.state)['status'], 'unknown')
        self.assertTrue(self.send()['replayed'])
        self.adapter.send.assert_called_once()

    def test_identity_mismatch_before_dispatch(self):
        for key, value in [('platform', 'wecom'), ('account', 'wrong-account'), ('chat', 'someone')]:
            previous = getattr(self.args, key)
            setattr(self.args, key, value)
            with self.assertRaises(QueryError):
                self.send()
            setattr(self.args, key, previous)
        self.settings['wechat']['account_key'] = 'wrong-account'
        with self.assertRaises(QueryError):
            self.send()
        self.adapter.send.assert_not_called()

    def test_wecom_routes_to_its_own_adapter(self):
        self.args.chat = 'wecom:chat:7'
        self.chat.update(platform='wecom', chat_ref=self.args.chat, chat_id='synthetic-wecom-conv')
        self.settings['wecom'] = self.settings.pop('wechat')
        self.send()
        self.factory.assert_called_once_with('wecom', self.settings['wecom'])
        self.adapter.send.assert_called_once_with('synthetic-wecom-conv', self.args.text, self.args.request_id)

    def test_invalid_or_unready_never_dispatches(self):
        for text in ['', ' ', '\x00', '中' * 6000, None, '\ud800']:
            self.args.text = text
            with self.assertRaises(QueryError):
                self.send()
        self.args.text = 'synthetic'
        for request in ['', 'bad id', 'a' * 129]:
            self.args.request_id = request
            with self.assertRaises(QueryError):
                self.send()
        self.args.request_id = 'synthetic-1'
        for reason in ['wechat_not_running', 'unsupported_wechat_build', 'backend_state_expired', 'sender_account_mismatch']:
            self.adapter.health.return_value = dict(ready=False, status=reason)
            self.assertEqual(self.send()['error'], reason)
        self.adapter.send.assert_not_called()

    def test_receipt_identity_filter_and_missing_read_only(self):
        with self.assertRaises(QueryError):
            sending.send_status('missing', state=self.state)
        self.assertFalse(self.state.exists())
        self.send()
        for filters in [dict(platform='wecom'), dict(account='another-account')]:
            with self.assertRaises(QueryError):
                sending.send_status('synthetic-1', state=self.state, **filters)

    def test_symlink_receipts_rejected(self):
        self.state.mkdir()
        target = Path(self.tmp.name) / 'target'
        target.touch()
        (self.state / 'receipts.sqlite3').symlink_to(target)
        with self.assertRaises(QueryError):
            self.send()
        self.adapter.send.assert_not_called()

    def test_cli_mcp_share_dispatch_and_side_effect_annotations(self):
        receipt = dict(platform='wechat', account_key='synthetic-account', status='server_accepted', request_id='r1')
        with patch('chatlog_core.sending.send', return_value=receipt) as send:
            result, code, images = execute(['send', '--chat', 'wechat:chat:7', '--text=--raw', '--request-id', 'r1'])
            wire = invoke('send', dict(chat='wechat:chat:7', text='--raw', request_id='r1'))
        self.assertEqual(code, 0)
        self.assertEqual(wire.structured_content, result)
        self.assertEqual([call.args[0].text for call in send.call_args_list], ['--raw', '--raw'])
        for tool in TOOLS:
            self.assertEqual(tool.annotations.read_only_hint, tool.name not in ('send', 'download'))
            self.assertEqual(tool.annotations.open_world_hint, tool.name in ('send', 'semantic_search'))
        self.assertEqual(set(DEFINITIONS['send']['schema']['required']), {'chat', 'request_id'})
        self.assertEqual(DEFINITIONS['send']['schema']['anyOf'], [{'required':['text']}, {'required':['attachments']}])

    def test_bad_mcp_sends_do_not_dispatch(self):
        with patch('chatlog_core.mcp_server.execute', side_effect=AssertionError('must not execute')):
            for args in [{}, dict(chat='nickname', text='test', request_id='r1'),
                         dict(chat='wechat:chat:7', text='test'),
                         dict(chat='wechat:chat:7', text='test', request_id='bad id')]:
                self.assertTrue(invoke('send', args).is_error)

    def test_malformed_optional_settings_do_not_break_status(self):
        path = Path(self.tmp.name) / 'sending.json'
        for binding in [[], None, {}, {'account_key': 1, 'self_id': 'synthetic'}]:
            path.write_text(json.dumps({'version': 1, 'platforms': {'wechat': binding}}))
            path.chmod(0o600)
            with self.assertRaises(QueryError):
                sending.load_settings(path)
        with patch.object(sending, 'load_settings', side_effect=QueryError('invalid_sending_config', 'synthetic')):
            self.assertTrue(all(not row['ready'] for row in sending.status().values()))

    def test_adapter_rechecks_bound_identity_at_dispatch(self):
        class BackendError(Exception):
            pass
        module = Mock(SendError=BackendError)
        module.load_config.return_value = {'self_id': 'synthetic-account'}
        module.send_message.return_value = {'status': 'server_accepted'}
        with patch.object(sending, 'native_module', return_value=module):
            adapter = sending.NativeAdapter('wechat', self.settings['wechat'])
            adapter.send('synthetic-user', 'synthetic', 'r1')
            module.send_message.assert_called_once_with('synthetic-user', 'synthetic', request_id='r1', expected_self_id='synthetic-account')
            module.load_config.return_value = {'self_id': 'another-account'}
            with self.assertRaises(QueryError):
                sending.NativeAdapter('wechat', self.settings['wechat'])

    def test_unknown_cli_result_and_readonly_status_have_distinct_exit_codes(self):
        receipt = {'status': 'unknown', 'request_id': 'r1', 'error': 'native_result_unknown'}
        with patch.object(sending, 'send', return_value=receipt):
            result, code, _ = execute(['send', '--chat', 'wechat:chat:7', '--text', 'synthetic', '--request-id', 'r1'])
        self.assertEqual(code, 4)
        self.assertFalse(result['ok'])
        with patch.object(sending, 'send_status', return_value=receipt), \
             patch('chatlog_core.cli.load_config', side_effect=AssertionError('no database needed')):
            result, code, _ = execute(['send_status', 'r1'])
        self.assertEqual(code, 0)
        self.assertEqual(result['data']['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
