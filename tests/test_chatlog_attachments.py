"""Attachment discovery/export regression using disposable synthetic databases only."""
import concurrent.futures
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'lib'), str(ROOT / 'tests')]
import test_chatlog as fixtures
from chatlog_core import attachments
from chatlog_core.cli import execute, parser
from chatlog_core.common import QueryError
from chatlog_core.mcp_server import invoke, TOOLS
from chatlog_core.query import Reader, connect, select
from mysql_store import conn as admin


class AttachmentTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DatabaseTests.setUpClass.__func__)
    cleanup = classmethod(fixtures.DatabaseTests.cleanup.__func__)
    seed = classmethod(fixtures.DatabaseTests.seed.__func__)

    def setUp(self):
        self.config = dict(host='127.0.0.1', port=int(os.environ.get('WX_MYSQL_PORT', 3306)),
                           user=self.user, password=self.password)
        self.root = Path(self.temp.name)
        self.payload = b'%PDF-synthetic-only\n' + '合成文件'.encode() * 500
        self.ids = {}
        for p, db in self.databases.items():
            with admin.connect(db) as c:
                # Only this test's file records; existing fixture images are preserved.
                admin.execute(c, "DELETE FROM blob_parts WHERE table_name='media_items' AND row_id IN "
                              "(SELECT id FROM media_items WHERE msg_kind='file')")
                admin.execute(c, "DELETE FROM media_items WHERE msg_kind='file'")
                with c.cursor() as cur:
                    cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,file_name,bytes,'
                                'sha1,content_md5,data) VALUES(%s,%s,1,%s,%s,%s,%s,%s,%s)',
                                [p, 'fixture_a', 'file', '../../合成 %_!.pdf', len(self.payload),
                                 hashlib.sha1(self.payload).hexdigest(), hashlib.md5(self.payload).hexdigest(), self.payload])
                    self.ids[p] = cur.lastrowid
                c.commit()

    def reader(self, argv):
        reader = Reader(self.config, parser().parse_args(argv), databases=self.databases, root=self.root)
        self.addCleanup(reader.close)
        return reader

    def call(self, argv):
        def make(config, args):
            return Reader(config, args, databases=self.databases, root=self.root)
        with patch('chatlog_core.cli.Reader', side_effect=make), patch('chatlog_core.cli.load_config', return_value=self.config):
            return execute(argv)

    def ref(self, p='wechat'):
        return f'{p}:media:{self.ids[p]}'

    def test_index_keyword_kind_account_and_exact_references(self):
        reader = self.reader(['attachments', '%_!', '--kind', 'file', '--account', 'fixture_a'])
        rows, page = attachments.listing(reader)
        self.assertFalse(reader.errors)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row['chat_ref'], row['platform'] + ':chat:1')
            self.assertEqual(row['message_ref'], row['platform'] + ':message:1')
            self.assertTrue(row['download']['available'])
            self.assertEqual(row['download']['command'][2], row['media_ref'])
        self.assertFalse(page['has_more'])
        rows, _ = attachments.listing(self.reader(['attachments', '--kind', 'file', '--account', 'fixture_b']))
        self.assertEqual(rows, [])

    def test_index_pagination_cross_platform_and_kind_is_media_kind(self):
        first, page = attachments.listing(self.reader(['attachments', '--kind', 'file', '--limit', '1']))
        second, _ = attachments.listing(self.reader(['attachments', '--kind', 'file', '--limit', '1', '--cursor', page['next_cursor']]))
        self.assertEqual(len(first + second), 2)
        self.assertNotEqual(first[0]['media_ref'], second[0]['media_ref'])

    def test_index_includes_unlinked_directory_media_without_inventing_chat(self):
        with admin.connect(self.databases['wechat']) as c:
            with c.cursor() as cur:
                cur.execute("INSERT INTO media_items(platform,account_key,msg_kind,file_name,data,bytes,ts) "
                            "VALUES('wechat','fixture_a','file','目录附件.pdf',%s,%s,300)",
                            [self.payload, len(self.payload)])
                pk = cur.lastrowid
            c.commit()
        rows, _ = attachments.listing(self.reader(['attachments', '目录附件', '--platform', 'wechat']))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['media_ref'], f'wechat:media:{pk}')
        self.assertIsNone(rows[0]['chat_ref'])
        self.assertIsNone(rows[0]['message_ref'])

    def test_download_blob_hash_and_mcp_clickable_link(self):
        result, code, _ = self.call(['download', self.ref(), '--json'])
        self.assertEqual(code, 0, result)
        row = result['data'][0]
        target = Path(row['output_path'])
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertTrue(target.is_relative_to(self.root / '.runtime/chatlog-downloads'))
        self.assertNotIn('..', target.name)
        self.assertEqual(row['download']['verified_against_index'], ['sha1', 'md5'])
        with patch('chatlog_core.mcp_server.execute', return_value=(result, code, [])):
            mcp = invoke('download', {'reference': self.ref()})
        self.assertEqual(mcp.content[-1].type, 'resource_link')
        self.assertEqual(str(mcp.content[-1].uri), target.as_uri())

    def test_repeat_download_reuses_same_verified_file(self):
        first, code, _ = self.call(['download', self.ref()])
        self.assertEqual(code, 0)
        second, code, _ = self.call(['download', self.ref()])
        self.assertEqual(code, 0, second)
        self.assertEqual(first['data'][0]['output_path'], second['data'][0]['output_path'])
        self.assertTrue(second['data'][0]['download']['reused'])

    def test_concurrent_downloads_publish_once(self):
        def run(_):
            reader = Reader(self.config, parser().parse_args(['download', self.ref('wecom')]),
                            databases=self.databases, root=self.root)
            try:
                return attachments.export(reader)[0][0]['output_path']
            finally:
                reader.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            paths = list(pool.map(run, range(3)))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(Path(paths[0]).read_bytes(), self.payload)

    def test_chunked_blob_export(self):
        pk = self.ids['wechat']
        with admin.connect(self.databases['wechat']) as c:
            admin.execute(c, 'UPDATE media_items SET data=NULL WHERE id=%s', [pk])
            for i, block in enumerate((self.payload[:9], self.payload[9:])):
                admin.execute(c, "INSERT INTO blob_parts(table_name,row_id,part_no,data) VALUES('media_items',%s,%s,%s)", [pk, i, block])
            c.commit()
        result, code, _ = self.call(['download', self.ref()])
        self.assertEqual(code, 0, result)
        self.assertEqual(Path(result['data'][0]['output_path']).read_bytes(), self.payload)

    def test_missing_part_is_not_a_download(self):
        with admin.connect(self.databases['wechat']) as c:
            admin.execute(c, 'UPDATE media_items SET data=NULL WHERE id=%s', [self.ids['wechat']])
            admin.execute(c, "INSERT INTO blob_parts(table_name,row_id,part_no,data) VALUES('media_items',%s,1,%s)", [self.ids['wechat'], self.payload])
            c.commit()
        result, code, _ = self.call(['download', self.ref()])
        self.assertEqual(code, 4)
        self.assertEqual(result['errors'][0]['code'], 'media_parts_incomplete')

    def test_integrity_failure_and_original_unchanged(self):
        with admin.connect(self.databases['wechat']) as c:
            admin.execute(c, 'UPDATE media_items SET sha1=%s WHERE id=%s', ['0' * 40, self.ids['wechat']])
            c.commit()
        result, code, _ = self.call(['download', self.ref()])
        self.assertEqual(code, 4)
        self.assertEqual(result['errors'][0]['code'], 'media_integrity_failed')
        with connect(self.config, self.databases['wechat']) as c:
            self.assertEqual(select(c, 'SELECT data FROM media_items WHERE id=%s', [self.ids['wechat']])[0]['data'], self.payload)

    def test_missing_account_mismatch_and_platform_mismatch(self):
        result, code, _ = self.call(['download', self.ref(), '--account', 'fixture_b'])
        self.assertEqual(code, 3)
        result, code, _ = self.call(['download', self.ref(), '--platform', 'wecom'])
        self.assertEqual(result['errors'][0]['code'], 'platform_mismatch')
        with admin.connect(self.databases['wechat']) as c:
            admin.execute(c, 'UPDATE media_items SET data=NULL,unavailable=1 WHERE id=%s', [self.ids['wechat']])
            c.commit()
        result, code, _ = self.call(['download', self.ref()])
        self.assertEqual(code, 4)
        self.assertEqual(result['errors'][0]['code'], 'media_not_cached')

    def test_export_existing_output_and_media_store_are_protected(self):
        target = self.root / 'preserve.txt'
        target.write_bytes(b'keep')
        result, code, _ = self.call(['download', self.ref(), '--out', str(target)])
        self.assertEqual(result['errors'][0]['code'], 'output_exists')
        self.assertEqual(target.read_bytes(), b'keep')
        target = self.root / 'media_store/images/forbidden.pdf'
        result, code, _ = self.call(['download', self.ref(), '--out', str(target)])
        self.assertEqual(result['errors'][0]['code'], 'invalid_output_path')

    def test_file_path_export_and_traversal_rejected(self):
        source = self.root / 'media_store/images/attachment.pdf'
        source.write_bytes(self.payload)
        with admin.connect(self.databases['wechat']) as c:
            admin.execute(c, 'UPDATE media_items SET data=NULL,file_path=%s,file_sha256=%s WHERE id=%s',
                          ['images/attachment.pdf', hashlib.sha256(self.payload).hexdigest(), self.ids['wechat']])
            c.commit()
        result, code, _ = self.call(['download', self.ref()])
        self.assertEqual(code, 0, result)
        self.assertEqual(source.read_bytes(), self.payload)
        with admin.connect(self.databases['wechat']) as c:
            admin.execute(c, 'UPDATE media_items SET file_path=%s WHERE id=%s', ['../../outside.pdf', self.ids['wechat']])
            c.commit()
        result, code, _ = self.call(['download', self.ref()])
        self.assertEqual(result['errors'][0]['code'], 'invalid_media_path')

    def test_message_ambiguity_and_mcp_out_not_allowed(self):
        # Message 2 already has multiple fixture images/voice.
        result, code, _ = self.call(['download', 'wechat:message:2'])
        self.assertEqual(code, 3)
        self.assertEqual(result['errors'][0]['code'], 'ambiguous_media')
        result = invoke('download', {'reference': self.ref(), 'out': '/tmp/forbidden'})
        self.assertTrue(result.is_error)
        tools = {t.name: t for t in TOOLS}
        self.assertTrue(tools['attachments'].annotations.read_only_hint)
        self.assertFalse(tools['download'].annotations.read_only_hint)
        self.assertFalse(tools['download'].annotations.open_world_hint)

    def test_image_blob_preview_is_returned_without_file_write(self):
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (20, 30), (30, 40, 50)).save(image, format='PNG')
        with admin.connect(self.databases['wechat']) as c:
            with c.cursor() as cur:
                cur.execute("INSERT INTO media_items(platform,account_key,message_pk,msg_kind,data,bytes) VALUES('wechat','fixture_a',1,'thumb',%s,%s)",
                            [image.getvalue(), len(image.getvalue())])
                pk = cur.lastrowid
            c.commit()
        try:
            result, code, images = self.call(['media', f'wechat:media:{pk}'])
            self.assertEqual(code, 0, result)
            self.assertEqual(len(images), 1)
            self.assertEqual(result['data'][0]['preview']['preview_width'], 20)
        finally:
            with admin.connect(self.databases['wechat']) as c:
                admin.execute(c, 'DELETE FROM media_items WHERE id=%s', [pk])
                c.commit()


if __name__ == '__main__':
    unittest.main()
