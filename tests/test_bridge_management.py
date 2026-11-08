"""CLI management and dual-runner tests; synthetic identities, no live APIs."""
import argparse
import copy
import os
import unittest
import contextlib
import io
import json
import threading
from unittest.mock import patch
import test_chatlog_bridge as fixtures
from chatlog_core.bridge_manage import manage
from chatlog_core.common import QueryError

DSH='session-synthetic-dsh'
THREAD=fixtures.THREAD
message=fixtures.message


class FakeDsh(fixtures.FakeDesktop):
    def binding(self,thread,workspace):
        if thread!=DSH or workspace!='/fixture':raise QueryError('dsh_binding_mismatch','fixture')
    def submit(self,thread,workspace,prompt,request_id=None):
        self.binding(thread,workspace);self.calls.append((thread,workspace,prompt,request_id));return dict(status='dsh_queued',native_message_id=request_id)


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.BridgeTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        for key in ('bridge','store','feed','desktop'):setattr(self,key,getattr(self.fixture,key))
        self.dsh=FakeDsh();self.bridge.runners['dsh']=self.dsh
    def last(self):return self.fixture.last()
    def cmd(self,action,**kw):
        return manage(self.bridge,argparse.Namespace(action=action,**kw))
    def bind(self):
        self.cmd('bind',person='friend',backend='dsh',workspace='/fixture',session=DSH)
    def switch(self,backend='dsh'):
        return self.cmd('backend',backend=backend,all=True,person=None)

    def test_bind_then_switch_and_switch_back(self):
        self.bind();before=self.store.config()['principals'][0]
        self.assertEqual(before['thread_id'],THREAD)
        self.switch();active=self.store.config()['principals'][0]
        self.assertEqual(active['thread_id'],DSH);self.assertIn('codex',active['targets'])
        self.assertEqual(self.bridge.deliver(message()),'dsh_queued');self.assertEqual(len(self.desktop.calls),0)
        self.assertEqual(self.dsh.calls[0][0],DSH)
        self.switch('codex');self.assertEqual(self.store.config()['principals'][0]['thread_id'],THREAD)

    def test_dsh_reply_requires_dsh_session_id(self):
        self.bind();self.switch();self.bridge.deliver(message());record=self.last()
        with patch.dict(os.environ,DSH_SESSION_ID='wrong',CODEX_THREAD_ID=DSH):
            with self.assertRaises(QueryError):self.bridge.reply(record['token'],'answer',lambda *_:dict(ok=True))
        with patch.dict(os.environ,DSH_SESSION_ID=DSH):
            self.assertTrue(self.bridge.reply(record['token'],'answer',lambda *_:dict(ok=True))['ok'])

    def test_switch_does_not_replay_previously_delivered_message(self):
        self.bridge.deliver(message());record=self.last();self.bind();self.switch()
        self.assertEqual(self.bridge.deliver(message()),'duplicate')
        self.assertEqual(self.dsh.calls,[])
        with patch.dict(os.environ,CODEX_THREAD_ID=THREAD):
            with self.assertRaises(QueryError):self.bridge.reply(record['token'],'old answer')

    def test_switch_is_atomic_if_any_person_lacks_target(self):
        before=self.store.config()
        with self.assertRaises(QueryError):self.switch()
        self.assertEqual(self.store.config(),before)

    def test_stale_config_is_rejected(self):
        old=self.store.config();fresh=copy.deepcopy(old);fresh['paused']=True;self.bridge.save(fresh)
        with self.assertRaisesRegex(QueryError,'其他管理操作'):self.bridge.save(old)
        missing=copy.deepcopy(self.store.config());missing.pop('revision')
        with self.assertRaises(QueryError):self.bridge.save(missing)

    def test_owner_add_is_idempotent_and_disabled_until_bound(self):
        args=dict(people_action='add',owner_self=True,name='本人',wechat=None,wecom=None)
        first=manage(self.bridge,argparse.Namespace(action='people',**args));second=manage(self.bridge,argparse.Namespace(action='people',**args))
        self.assertEqual(first['id'],second['id']);self.assertTrue(second['already_exists'])
        with self.assertRaises(QueryError):self.cmd('people',people_action='enable',person=first['id'],confirm_identities=True)

    def test_notice_latency_only_attaches_to_actual_queued_request(self):
        import time
        self.bind();self.switch()
        self.bridge.notice(dict(platform='wechat',ids=[11],detected_unix=time.time()-.1))
        self.feed.items=[message()];self.bridge.poll()
        self.assertGreater(self.last()['dispatch_seconds'],0)
        self.assertEqual(self.last()['backend'],'dsh')

    def test_sandbox_reply_cli_uses_capability_endpoint_without_opening_store(self):
        from chatlog_core.bridge_web import server
        from chatlog_core import bridge_app
        self.bridge.deliver(message());record=self.last();original=self.bridge.reply
        self.bridge.reply=lambda token,text,caller_session_id=None:original(token,text,sender=lambda *_:dict(ok=True,data=dict(status='server_accepted')),caller_session_id=caller_session_id)
        httpd=server(self.bridge,0);worker=threading.Thread(target=httpd.serve_forever,daemon=True);worker.start()
        try:
            output=io.StringIO()
            with patch.dict(os.environ,CODEX_THREAD_ID=THREAD,DSH_SESSION_ID='',CHATLOG_BRIDGE_STATE=''),patch.object(bridge_app,'PORT',httpd.server_port),patch.object(bridge_app,'Store',side_effect=AssertionError('CLI must not open writable state')),contextlib.redirect_stdout(output):
                self.assertEqual(bridge_app.main(['reply',record['token'],'--text','answer','--json']),0)
            self.assertTrue(json.loads(output.getvalue())['ok'])
            with patch.dict(os.environ,CODEX_THREAD_ID='wrong',DSH_SESSION_ID=''),patch.object(bridge_app,'PORT',httpd.server_port):
                with self.assertRaises(QueryError):bridge_app.proxy_reply(record['token'],'answer')
        finally:httpd.shutdown();httpd.server_close();worker.join()

    def test_unhealthy_probe_recovers_without_waiting_healthy_interval(self):
        from chatlog_core.bridge_app import monitor_health
        from unittest.mock import Mock
        class Stop:
            def __init__(self):self.waits=[]
            def is_set(self):return len(self.waits)>=2
            def wait(self,seconds):self.waits.append(seconds)
        stop=Stop();wake=Mock()
        with patch('chatlog_core.sending.status',side_effect=[{'wechat':{'ready':False}},{'wechat':{'ready':True}}]):monitor_health(self.bridge,stop,wake,15)
        self.assertEqual(stop.waits,[.5,15]);self.assertTrue(self.bridge.sender_health['wechat']['ready'])
