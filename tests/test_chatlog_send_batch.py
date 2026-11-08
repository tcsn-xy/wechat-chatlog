"""Synthetic native adapters only: these tests cannot send real messages."""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chatlog_core import sending
from chatlog_core.cli import parser, execute
from chatlog_core.common import QueryError
from chatlog_core.mcp_server import invoke
from chatlog_core.send_payload import prepare


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.image = self.root / '成果🌱.png'
        Image.new('RGB', (8, 8), 'green').save(self.image)
        self.file = self.root / '合成文件.txt'
        self.file.write_bytes(b'synthetic fixture\n')
        self.args = parser().parse_args(['send', '--chat', 'wechat:chat:7', '--text', 'synthetic response',
            '--file', str(self.file), '--image', str(self.image), '--file', str(self.file), '--request-id', 'batch-1'])
        self.chat = dict(platform='wechat', account_key='fixture', chat_ref='wechat:chat:7', chat_id='fixture-target')
        self.settings = {'wechat': dict(account_key='fixture', self_id='fixture')}
        self.adapter = Mock()
        self.adapter.health.return_value = dict(ready=True)
        self.adapter.media_capabilities.return_value = {k:dict(supported=True, max_bytes=1024*1024) for k in ('image','file')}
        self.adapter.send.return_value = dict(status='server_accepted', task_id=5)
        self.adapter.send_attachment.return_value = dict(status='server_accepted', task_id=6, resources_released=True)
        self.factory = Mock(return_value=self.adapter)

    def send(self, **kwargs):
        return sending.send(self.args, lambda:self.chat, settings=self.settings, state=self.state,
                            adapter_factory=self.factory, **kwargs)

    def test_order_immutable_files_and_child_receipts(self):
        seen = []
        def native(target, item, rid):
            seen.append(item['type'])
            self.assertEqual(Path(item['path']).name, item['name'])
            self.assertEqual(Path(item['path']).stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(item['path'], str(self.file))
            if item['type']=='file':self.assertEqual(Path(item['path']).read_bytes(), self.file.read_bytes())
            return dict(status='server_accepted', resources_released=True)
        self.adapter.send_attachment.side_effect=native
        result=self.send()
        self.assertEqual(seen, ['file','image','file'])
        self.assertEqual(result['batch_status'],'complete')
        self.assertEqual(len(result['items']),4)
        for row in result['items']:
            receipt=sending.send_status(row['request_id'],state=self.state)
            self.assertEqual(receipt['status'],'server_accepted')
        self.assertEqual(list((self.state/'outgoing').iterdir()),[])
        raw=(self.state/'receipts.sqlite3').read_bytes()
        self.assertNotIn(b'synthetic response',raw)
        self.assertNotIn(str(self.file).encode(),raw)

    def test_concurrency_replays_without_dispatch(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.send(),range(4)))
        self.assertEqual(self.adapter.send.call_count,1)
        self.assertEqual(self.adapter.send_attachment.call_count,3)
        self.assertEqual(sum(bool(r.get('replayed')) for r in results),3)

    def test_modified_content_order_name_type_and_text_conflict(self):
        self.send()
        variants=[dict(text='changed'),dict(attachments=self.args.attachments[1:]+self.args.attachments[:1]),
                  dict(attachments=[dict(type='image',path=str(self.image))])]
        for changes in variants:
            old={k:getattr(self.args,k) for k in changes}
            for k,v in changes.items():setattr(self.args,k,v)
            with self.assertRaises(QueryError):self.send()
            for k,v in old.items():setattr(self.args,k,v)
        self.file.write_text('changed fixture')
        with self.assertRaises(QueryError):self.send()
        self.assertEqual(self.adapter.send.call_count,1)

    def test_all_files_validated_before_text_or_any_media(self):
        for path in [str(self.root/'missing'),str(self.root),'relative.txt','https://invalid.test/a']:
            self.args.attachments[-1]=dict(type='file',path=path)
            with self.assertRaises(QueryError):self.send()
        self.adapter.send.assert_not_called()
        self.adapter.send_attachment.assert_not_called()

    def test_invalid_image_symlink_fifo_empty_and_limit(self):
        empty=self.root/'empty';empty.touch()
        link=self.root/'link';link.symlink_to(self.file)
        fifo=self.root/'fifo';os.mkfifo(fifo)
        for kind,path in [('file',empty),('file',link),('file',fifo),('image',self.file)]:
            self.args.attachments=[dict(type=kind,path=str(path))]
            with self.assertRaises(QueryError):self.send()
        self.args.attachments=[dict(type='file',path=str(self.file))]*21
        with self.assertRaises(QueryError):self.send()
        self.adapter.send.assert_not_called()

    def test_truncated_jpeg_is_rejected_before_any_batch_submission(self):
        import io
        buffer=io.BytesIO();Image.new('RGB',(16,16),'green').save(buffer,format='JPEG')
        path=self.root/'truncated.jpg';path.write_bytes(buffer.getvalue()[:-10])
        # Header verification alone accepts this; full decoding must reject it.
        with Image.open(path) as image:image.verify()
        self.args.attachments.append(dict(type='image',path=str(path)))
        with self.assertRaises(QueryError):self.send()
        self.adapter.send.assert_not_called();self.adapter.send_attachment.assert_not_called()

    def test_platform_account_and_unsupported_type_preflight(self):
        self.args.account='wrong'
        with self.assertRaises(QueryError):self.send()
        self.args.account=None
        self.adapter.media_capabilities.return_value['image']['supported']=False
        with self.assertRaises(QueryError) as exc:self.send()
        self.assertEqual(exc.exception.detail['code'],'sending_type_unsupported')
        self.adapter.send.assert_not_called()

    def test_recycled_chat_ref_cannot_replay_as_a_different_raw_target(self):
        self.send();self.chat['chat_id']='different-target'
        with self.assertRaises(QueryError) as exc:self.send()
        self.assertEqual(exc.exception.detail['code'],'request_id_content_conflict')
        self.assertEqual(self.adapter.send_attachment.call_count,3)

    def test_partial_unknown_unconfirmed_and_not_attempted(self):
        for status,overall in [('not_sent','partial'),('unknown','unknown'),('submitted_unconfirmed','pending')]:
            self.args.request_id += 'x'
            self.adapter.send_attachment.reset_mock()
            self.adapter.send_attachment.return_value=dict(status=status)
            first=self.send()
            self.assertEqual(first['batch_status'],overall)
            self.assertTrue(first['items'][1]['attempted'])
            self.assertFalse(first['items'][2]['attempted'])
            self.assertTrue(self.send()['replayed'])
            self.assertEqual(self.adapter.send_attachment.call_count,1)
        self.assertTrue(list((self.state/'outgoing').iterdir()))

    def test_dispatch_crash_reserves_unknown_parent_and_children(self):
        self.adapter.send_attachment.side_effect=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.send()
        root=sending.send_status(self.args.request_id,state=self.state)
        self.assertEqual(root['status'],'unknown')
        self.assertEqual(sending.send_status(root['items'][1]['request_id'],state=self.state)['status'],'unknown')
        self.assertTrue(self.send()['replayed'])
        self.assertEqual(self.adapter.send_attachment.call_count,1)
        self.assertTrue(list((self.state/'outgoing').iterdir()))

    def test_revoke_stops_remaining_items(self):
        checks=[]
        def guard():
            checks.append(True)
            if len(checks)==3:raise QueryError('bridge_permission_revoked','fixture')
        result=self.send(before_item=guard)
        self.assertEqual(result['batch_status'],'partial')
        self.assertEqual(self.adapter.send_attachment.call_count,1)
        self.assertEqual(result['items'][2]['error'],'bridge_permission_revoked')

    def test_source_replaced_after_snapshot_does_not_change_native_bytes(self):
        original=self.file.read_bytes()
        with prepare([dict(type='file',path=str(self.file))],self.state) as payload:
            self.file.write_bytes(b'replacement')
            self.assertEqual(Path(payload['items'][0]['path']).read_bytes(),original)

    def test_cli_mcp_media_only_and_order_identical(self):
        receipt=dict(status='server_accepted',batch_status='complete',items=[])
        with patch('chatlog_core.sending.send',return_value=receipt) as send:
            cli=execute(['send','--chat','wechat:chat:7','--file',str(self.file),'--image',str(self.image),'--request-id','r'])[0]
            mcp=invoke('send',dict(chat='wechat:chat:7',request_id='r',attachments=[
                dict(type='file',path=str(self.file)),dict(type='image',path=str(self.image))]))
            self.assertEqual(cli,mcp.structured_content)
            self.assertEqual(send.call_args_list[0].args[0].attachments,send.call_args_list[1].args[0].attachments)
        for attachments in [[],[dict(type='file',path='relative')],[dict(type='voice',path='/x')]]:
            self.assertTrue(invoke('send',dict(chat='wechat:chat:7',request_id='r',attachments=attachments)).is_error)

    def test_partial_exit_code_five(self):
        receipt=dict(status='partial',batch_status='partial',items=[dict(status='server_accepted'),dict(status='not_sent')])
        with patch('chatlog_core.sending.send',return_value=receipt):
            result,code,_=execute(['send','--chat','wechat:chat:7','--file',str(self.file),'--request-id','r'])
        self.assertEqual(code,5)
        self.assertTrue(result['partial'])
        self.assertFalse(result['ok'])

    def test_late_correlated_ack_updates_receipts_without_resuming(self):
        self.adapter.send_attachment.return_value=dict(status='unknown')
        result=self.send();child=result['items'][1]['request_id']
        answer=dict(status='server_accepted',native_finished=True,task_id=44,ack_ret=0,resources_released=True)
        identity=dict(platform='wechat',account='fixture',target_id='fixture-target',state=self.state)
        self.assertFalse(sending.record_terminal_receipt(child,answer,**dict(identity,account='wrong')))
        self.assertFalse(sending.record_terminal_receipt(child,dict(answer,native_finished=False),**identity))
        self.assertTrue(sending.record_terminal_receipt(child,answer,**identity))
        root=sending.send_status(self.args.request_id,state=self.state)
        self.assertEqual(root['batch_status'],'partial')
        self.assertEqual(root['items'][1]['status'],'server_accepted')
        self.assertFalse(root['items'][2]['attempted'])
        self.assertEqual(self.send()['batch_status'],'partial')
        self.assertEqual(self.adapter.send_attachment.call_count,1)

    def test_late_ack_can_complete_a_single_attachment(self):
        self.args.text=None;self.args.attachments=self.args.attachments[:1]
        self.adapter.send_attachment.return_value=dict(status='submitted_unconfirmed')
        result=self.send()
        sending.record_terminal_receipt(result['items'][0]['request_id'],dict(status='server_accepted',native_finished=True,task_id=44,ack_ret=0),
            platform='wechat',account='fixture',target_id='fixture-target',state=self.state)
        self.assertEqual(sending.send_status(self.args.request_id,state=self.state)['batch_status'],'complete')


if __name__=='__main__':unittest.main()
