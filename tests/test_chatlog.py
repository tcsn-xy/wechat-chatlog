"""Synthetic-only integration tests in disposable repairtest_* databases."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'lib')]
import pymysql
from PIL import Image
from mysql_store import conn as admin, schema, files
from chatlog_core.cli import main, parser, capabilities, prepare_output
from chatlog_core.common import QueryError, load_config, timestamp, safe_error, TABLES, media_consent
from chatlog_core.query import Reader, connect, select
from install_chatlog import validate_grants
import voice_asr_core as asr


class UnitTests(unittest.TestCase):
    def test_options_before_and_after_command(self):
        for argv in (['--json','--platform','wechat','messages'], ['messages','--json','--platform','wechat']):
            args = parser().parse_args(argv)
            self.assertTrue(args.json)
            self.assertEqual(args.platform, 'wechat')
        self.assertEqual(parser().parse_args(['messages']).platform, 'all')

    def test_help_and_capabilities_need_no_config(self):
        with patch('chatlog_core.cli.load_config', side_effect=AssertionError('must not connect')):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['capabilities', '--json']), 0)
            result = json.loads(out.getvalue())
            self.assertTrue(result['data']['read_only'])
            self.assertEqual(result['data']['version'], '1.1.0')
            self.assertEqual(set(result['data']['commands']), {'status','chats','contacts','members','messages','search','context','message','media','capabilities'})
            self.assertIn('out', {item['name'] for item in result['data']['commands']['media']['parameters']})

    def test_media_consent_requires_private_optin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'consent.json'
            self.assertIsNone(media_consent(path))
            path.write_text(json.dumps({'image_external_consent': True}))
            path.chmod(0o644)
            self.assertIsNone(media_consent(path))
            path.chmod(0o600)
            self.assertTrue(media_consent(path)['image_external_consent'])
            path.write_text(json.dumps({'image_external_consent': False}))
            self.assertIsNone(media_consent(path))
            target = Path(directory) / 'real.json'
            target.write_text(json.dumps({'image_external_consent': True}))
            target.chmod(0o600)
            link = Path(directory) / 'link.json'
            link.symlink_to(target)
            self.assertIsNone(media_consent(link))

    def test_preview_output_path_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'media_store' / 'images').mkdir(parents=True)
            (root / 'previews').mkdir()
            with self.assertRaises(QueryError):
                prepare_output(str(root / 'media_store' / 'images' / 'x.jpg'), root)
            target = root / 'previews' / 'x.jpg'
            self.assertEqual(prepare_output(str(target), root), target)
            (root / 'previews' / 'link.jpg').symlink_to(root / 'previews' / 'missing.jpg')
            with self.assertRaises(QueryError):
                prepare_output(str(root / 'previews' / 'link.jpg'), root)
            with self.assertRaises(QueryError):
                prepare_output(str(root / 'nope' / 'x.jpg'), root)

    def test_invalid_arguments_json(self):
        for args in (['messages','--limit','201'], ['search',''], ['messages','--since','no-date'], ['message','wechat:message:1','--raw-offset','2'],
                     ['media'], ['media','wechat:media:1','--max-size','100'], ['media','wechat:media:1','--max-size','4096']):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args + ['--json']), 2)
            self.assertFalse(json.loads(out.getvalue())['ok'])

    def test_config_does_not_fallback_to_root(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            with self.assertRaises(QueryError):
                load_config(path)
            path.write_text(json.dumps(dict(user='root', host='127.0.0.1', port=3306, password='synthetic')))
            path.chmod(0o600)
            with self.assertRaises(QueryError):
                load_config(path)
            path.write_text(json.dumps(dict(user='chatlog_reader', host='127.0.0.1', port=3306, password='synthetic')))
            path.chmod(0o644)
            with self.assertRaises(QueryError):
                load_config(path)
            path.chmod(0o600)
            self.assertEqual(load_config(path)['user'], 'chatlog_reader')

    def test_time_and_error_redaction(self):
        self.assertEqual(timestamp('2026-10-01'), timestamp('2026-09-30T16:00:00Z'))
        error = safe_error(pymysql.err.OperationalError(3024, 'sensitive SQL'), 'wechat')
        self.assertEqual(error['code'], 'query_timeout')
        self.assertNotIn('sensitive', str(error))

    def test_excess_grants_rejected(self):
        for text in ('GRANT ALL PRIVILEGES ON *.* TO x', 'GRANT SELECT ON wx_chat.* TO x', 'GRANT role TO x', 'GRANT SELECT ON wx_chat.messages TO x WITH GRANT OPTION'):
            with self.assertRaises(QueryError):
                validate_grants([{'grant': text}])
        self.assertEqual(validate_grants([{'grant': 'GRANT SELECT ON `wx_chat`.`messages` TO x'}]), {('wx_chat','messages')})


class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tag = uuid.uuid4().hex[:12]
        cls.databases = {p: 'repairtest_chatlog_' + cls.tag + '_' + suffix for p, suffix in [('wechat','wx'), ('wecom','wc')]}
        cls.user = 'cltest_' + cls.tag
        cls.password = uuid.uuid4().hex
        # CI 的 MySQL 是 Docker service，端口映射后服务端看到的来源不是 127.0.0.1。
        # 这个测试账号只在一次运行内存在，授权也只覆盖临时库，所以用 '%' 兼容本机与 CI。
        cls.grant_host = '%'
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.cleanup)
        media_root = Path(cls.temp.name) / 'media_store' / 'images'
        media_root.mkdir(parents=True)
        Image.new('RGB', (800, 400), (200, 40, 40)).save(media_root / 'synthetic.png')
        Image.new('RGB', (800, 400), (40, 200, 40)).save(media_root / 'synthetic_decoded.jpg')
        (media_root / 'synthetic-not-image.bin').write_bytes(b'synthetic bytes, not an image')
        with admin.connect() as c:
            with c.cursor() as cur:
                cur.execute('CREATE USER %s@%s IDENTIFIED BY %s', [cls.user, cls.grant_host, cls.password])
            for p, db in cls.databases.items():
                schema.create_all(c, db)
                with admin.connect(db) as fixture:
                    files.prepare(fixture)
                    asr.prepare(fixture)
                    for table in TABLES:
                        with c.cursor() as cur:
                            cur.execute(f'GRANT SELECT ON `{db}`.`{table}` TO %s@%s', [cls.user,cls.grant_host])
                    cls.seed(fixture, p)

    @classmethod
    def cleanup(cls):
        with admin.connect() as c, c.cursor() as cur:
            for db in cls.databases.values():
                cur.execute('DROP DATABASE IF EXISTS `' + db + '`')
            cur.execute('DROP USER IF EXISTS %s@%s', [cls.user,cls.grant_host])
        cls.temp.cleanup()

    @classmethod
    def seed(cls, c, p):
        with c.cursor() as cur:
            for account in ('fixture_a','fixture_b'):
                cur.execute('INSERT INTO accounts(platform,account_key,display_name) VALUES(%s,%s,%s)', [p,account,account])
            for pk, account, title in [(1,'fixture_a','同名群'),(2,'fixture_a','同名群'),(3,'fixture_b','另一个账号')]:
                cur.execute('INSERT INTO chats(chat_pk,platform,account_key,chat_id,title,last_ts) VALUES(%s,%s,%s,%s,%s,100)', [pk,p,account,'room'+str(pk),title])
            cur.execute('INSERT INTO contacts(platform,account_key,contact_id,nick_name,remark) VALUES(%s,%s,%s,%s,%s)', [p,'fixture_a','sender','昵称','备注'])
            cur.execute('INSERT INTO chat_members(platform,chat_pk,chat_id,contact_id,room_nick,shown) VALUES(%s,1,%s,%s,%s,%s)', [p,'room1','sender','群昵称','备注'])
            for pk in range(1,9):
                chat = 1 if pk <= 6 else (2 if pk == 7 else 3)
                account = 'fixture_b' if pk == 8 else 'fixture_a'
                text = '合同 已解压 %_! 中文' if pk == 2 else '消息' + str(pk)
                cur.execute('INSERT INTO messages(id,platform,account_key,chat_pk,chat_id,local_id,local_type,kind,sender_id,sender_name,ts,text,source_dataset,source_db,source_table) '
                            'VALUES(%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s)',
                            [pk,p,account,chat,'room'+str(chat),pk,'text','sender','备注',100 if 2 <= pk <= 5 else 95+pk,text,'fixture','message_0.db','Msg_fixture'])
                cur.execute('INSERT INTO message_raw(message_pk,platform,raw_content) VALUES(%s,%s,%s)', [pk,p,'原文'+str(pk)+'abcdef'])
            cur.execute('INSERT INTO message_links(platform,message_pk,chat_id,ts,url,url_hash) VALUES(%s,2,%s,100,%s,%s)', [p,'room1','https://example.invalid','a'*40])
            cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,file_path,file_sha256) VALUES(%s,%s,2,%s,%s,%s)', [p,'fixture_a','image','missing.png','a'*64])
            cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,file_path,file_sha256) VALUES(%s,%s,2,%s,%s,%s)', [p,'fixture_a','image','../../outside.png','b'*64])
            cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,data) VALUES(%s,%s,2,%s,%s)', [p,'fixture_a','voice',b'fake-voice'])
            cur.execute('INSERT INTO image_captions(platform,account_key,image_sha256,prompt_version,endpoint,status,caption,image_text) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                        [p,'fixture_a','a'*64,'fixture','local','complete','合成发票','税号123'])
            cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,ext,file_path,file_sha256) VALUES(%s,%s,3,%s,%s,%s,%s)',
                        [p,'fixture_a','voice','wav','synthetic.wav','c'*64])
            voice_id = cur.lastrowid
            # 合成图片媒体：可视图（decoded 优先）、非图片、缺失文件，供 media 命令测试。
            cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,file_path,decoded_path,file_sha256,decoded_sha256) '
                        'VALUES(%s,%s,4,%s,%s,%s,%s,%s)',
                        [p,'fixture_a','image','images/synthetic.png','images/synthetic_decoded.jpg','f'*64,'e'*64])
            cur.execute('INSERT INTO image_captions(platform,account_key,image_sha256,prompt_version,endpoint,status,caption,image_text) '
                        'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                        [p,'fixture_a','e'*64,'fixture','local','complete','合成可视图','预览测试'])
            cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,file_path,file_sha256) VALUES(%s,%s,5,%s,%s,%s)',
                        [p,'fixture_a','image','images/synthetic-not-image.bin','d'*64])
            cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,file_path,file_sha256) VALUES(%s,%s,5,%s,%s,%s)',
                        [p,'fixture_a','image','images/absent.jpg','b'*64])
            for _ in range(2):
                cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,file_path,file_sha256) VALUES(%s,%s,6,%s,%s,%s)',
                            [p,'fixture_a','image','images/synthetic.png','f'*64])
            cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,file_path,file_sha256) VALUES(%s,%s,7,%s,%s,%s)',
                        [p,'fixture_a','image','images/synthetic.png','f'*64])
            cur.execute('INSERT INTO media_items(platform,account_key,message_pk,msg_kind,file_path,file_sha256) VALUES(%s,%s,7,%s,%s,%s)',
                        [p,'fixture_a','image','images/absent7.jpg','b'*64])
        c.commit()
        asr.seed_batch(c, dict(batch_id='synthetic-voice', items={p:[dict(id=voice_id,platform=p,account_key='fixture_a',
            message_pk=3,ext='wav',file_path='synthetic.wav',file_sha256='c'*64)]}), 'd'*64, p)
        admin.execute(c, "UPDATE voice_transcripts SET status='complete',text=%s WHERE account_key='fixture_a'", ('合成语音 English %_!',))
        c.commit()

    def reader(self, argv, connector=connect):
        config = dict(host='127.0.0.1', port=int(os.environ.get('WX_MYSQL_PORT',3306)), user=self.user, password=self.password)
        reader = Reader(config, parser().parse_args(argv), connector=connector, databases=self.databases, root=self.temp.name)
        self.addCleanup(reader.close)
        return reader

    def run_query(self, argv):
        reader = self.reader(argv)
        data, page = reader.run()
        self.assertEqual(reader.errors, [])
        return data, page

    def test_all_platforms_and_same_second_pagination(self):
        refs = []
        cursor = None
        while True:
            args = ['messages','--limit','3'] + (['--cursor',cursor] if cursor else [])
            data, page = self.run_query(args)
            self.assertEqual([(r['ts'],r['platform'],r['id']) for r in data], sorted((r['ts'],r['platform'],r['id']) for r in data))
            refs += [r['message_ref'] for r in data]
            cursor = page['next_cursor']
            if not cursor:
                break
        self.assertEqual(len(refs), 16)
        self.assertEqual(len(set(refs)), 16)

    def test_ambiguous_names_and_account_boundary(self):
        with self.assertRaises(QueryError) as exc:
            self.reader(['messages','--chat','同名群']).run()
        self.assertEqual(exc.exception.detail['code'], 'ambiguous_chat')
        self.assertEqual(len(exc.exception.detail['candidates']), 4)
        rows, _ = self.run_query(['messages','--chat','wechat:chat:1'])
        self.assertEqual(len(rows), 6)
        self.assertEqual({r['platform'] for r in rows}, {'wechat'})
        rows, _ = self.run_query(['messages','--account','fixture_b'])
        self.assertEqual(len(rows), 2)
        with self.assertRaises(QueryError):
            self.reader(['message','wechat:message:1','--account','fixture_b']).run()

    def test_filters_literal_search_and_injection(self):
        for word in ('合同', '%_!'):
            rows, _ = self.run_query(['search',word])
            self.assertEqual(len(rows), 2)
        rows, _ = self.run_query(['search', "' OR 1=1 --"])
        self.assertEqual(rows, [])
        rows, _ = self.run_query(['messages','--platform','wechat','--chat','wechat:chat:1','--sender','sender','--kind','text',
                                 '--since','1970-01-01T00:01:40Z','--until','1970-01-01T00:01:41Z'])
        self.assertEqual([r['id'] for r in rows], [2,3,4,5])

    def test_context_preserves_chat_and_account(self):
        rows, _ = self.run_query(['context','wechat:message:3','--before','1','--after','2'])
        self.assertEqual([r['id'] for r in rows], [2,3,4,5])
        self.assertEqual([r['id'] for r in rows if r['is_anchor']], [3])

    def test_raw_links_media_and_caption(self):
        rows, _ = self.run_query(['message','wechat:message:2','--raw','--raw-limit','3'])
        row = rows[0]
        self.assertEqual(row['raw']['raw_content'], '原文2')
        self.assertEqual(row['raw']['next_offset'], 3)
        self.assertEqual(len(row['links']), 1)
        self.assertEqual(row['media'][0]['file_status'], 'missing')
        self.assertEqual(row['media'][0]['caption_status'], 'complete')
        self.assertEqual(row['media'][1]['file_status'], 'invalid_path')
        self.assertIsNone(row['media'][1]['file_path'])
        self.assertEqual(row['media'][2]['storage'], 'database_blob')
        self.assertNotIn('data', row['media'][2])
        rows, _ = self.run_query(['search','发票','--scope','caption'])
        self.assertEqual(len(rows), 2)
        rows, _ = self.run_query(['search','原文2','--scope','raw'])
        self.assertEqual(len(rows), 2)

    def test_media_command_previews_and_caption(self):
        rows, _ = self.run_query(['media','wechat:message:4'])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row['viewable'])
        self.assertTrue(row['preview']['available'])
        self.assertEqual((row['preview']['preview_width'], row['preview']['preview_height']), (800, 400))
        self.assertEqual(row['preview']['mime'], 'image/jpeg')
        self.assertEqual(row['preview']['decode_path'], 'direct')
        self.assertEqual(row['captions'][0]['caption'], '合成可视图')
        self.assertEqual(row['message_ref'], 'wechat:message:4')
        self.assertEqual(row['chat_ref'], 'wechat:chat:1')
        # 消息详情里的 media 行带 viewable 提示，但不含图片字节。
        detail, _ = self.run_query(['message','wechat:message:4'])
        self.assertTrue(detail[0]['media'][0]['viewable'])
        self.assertNotIn('preview', detail[0]['media'][0])
        # 单媒体引用与 --max-size 缩放。
        rows, _ = self.run_query(['media', row['media_ref'], '--max-size','256'])
        self.assertEqual((rows[0]['preview']['preview_width'], rows[0]['preview']['preview_height']), (256, 128))

    def test_media_cli_out_writes_preview_and_rejects_media_store(self):
        rows, _ = self.run_query(['media','wechat:message:4'])
        ref = rows[0]['media_ref']
        config = dict(host='127.0.0.1', port=int(os.environ.get('WX_MYSQL_PORT',3306)), user=self.user, password=self.password)

        def make_reader(_config, args):
            return Reader(_config, args, databases=self.databases, root=self.temp.name)

        out_dir = Path(self.temp.name) / 'previews'
        out_dir.mkdir()
        target = out_dir / 'preview.jpg'
        with patch('chatlog_core.cli.load_config', return_value=config), patch('chatlog_core.cli.Reader', side_effect=make_reader), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = main(['media', ref, '--out', str(target), '--json'])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result['data'][0]['output_path'], str(target))
        self.assertEqual(result['data'][0]['output_bytes'], target.stat().st_size)
        with Image.open(target) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (800, 400))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        forbidden = Path(self.temp.name) / 'media_store' / 'images' / 'preview.jpg'
        with patch('chatlog_core.cli.load_config', return_value=config), patch('chatlog_core.cli.Reader', side_effect=make_reader), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = main(['media', ref, '--out', str(forbidden), '--json'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['errors'][0]['code'], 'invalid_output_path')
        self.assertFalse(forbidden.exists())

    def test_media_out_requires_single_image(self):
        rows, _ = self.run_query(['media','wechat:message:6'])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['preview']['available'] for row in rows))
        config = dict(host='127.0.0.1', port=int(os.environ.get('WX_MYSQL_PORT',3306)), user=self.user, password=self.password)

        def make_reader(_config, args):
            return Reader(_config, args, databases=self.databases, root=self.temp.name)

        target = Path(self.temp.name) / 'multi.jpg'
        with patch('chatlog_core.cli.load_config', return_value=config), patch('chatlog_core.cli.Reader', side_effect=make_reader), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = main(['media','wechat:message:6','--out',str(target),'--json'])
        self.assertEqual(code, 2)
        result = json.loads(out.getvalue())
        self.assertEqual([error['code'] for error in result['errors']], ['out_requires_single_image'])
        self.assertFalse(result['ok'])
        self.assertFalse(target.exists())

    def test_media_partial_success_keeps_viewable_and_warns(self):
        reader = self.reader(['media','wechat:message:7'])
        rows, _ = reader.run()
        self.assertEqual(reader.errors, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(1 for row in rows if row['preview']['available']), 1)
        self.assertTrue(reader.warnings)
        config = dict(host='127.0.0.1', port=int(os.environ.get('WX_MYSQL_PORT',3306)), user=self.user, password=self.password)

        def make_reader(_config, args):
            return Reader(_config, args, databases=self.databases, root=self.temp.name)

        with patch('chatlog_core.cli.load_config', return_value=config), patch('chatlog_core.cli.Reader', side_effect=make_reader), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = main(['media','wechat:message:7','--json'])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])
        self.assertTrue(result['warnings'])

    def test_media_unavailable_rows_reported_not_crashed(self):
        with self.assertRaises(QueryError) as exc:
            self.reader(['media','wechat:message:1']).run()
        self.assertEqual(exc.exception.detail['code'], 'no_image_media')
        with self.assertRaises(QueryError) as exc:
            self.reader(['media','wechat:media:999999']).run()
        self.assertEqual(exc.exception.detail['code'], 'media_not_found')
        with self.assertRaises(QueryError) as exc:
            self.reader(['media','wechat:chat:1']).run()
        self.assertEqual(exc.exception.detail['code'], 'invalid_reference')
        reader = self.reader(['media','wechat:message:5'])
        rows, _ = reader.run()
        self.assertEqual(len(rows), 2)
        self.assertFalse(any(row['preview']['available'] for row in rows))
        codes = {row['preview']['error']['code'] for row in rows}
        self.assertIn('image_unavailable', codes)
        self.assertIn('media_not_image', codes)
        self.assertEqual(reader.errors[0]['code'], 'media_not_image')
        detail, _ = self.run_query(['message','wechat:message:3'])
        reader = self.reader(['media', detail[0]['media'][0]['media_ref']])
        rows, _ = reader.run()
        self.assertEqual(rows[0]['preview']['error']['code'], 'image_unavailable')

    def test_contacts_members_and_missing_message(self):
        rows, _ = self.run_query(['contacts','备注'])
        self.assertEqual(len(rows), 2)
        rows, _ = self.run_query(['members','--chat','wechat:chat:1'])
        self.assertEqual(rows[0]['room_nick'], '群昵称')
        with self.assertRaises(QueryError):
            self.reader(['message','wechat:message:999']).run()

    def test_voice_search_details_and_literal_patterns(self):
        for keyword in ('合成语音', '%_!'):
            rows, _ = self.run_query(['search',keyword,'--scope','voice'])
            self.assertEqual(len(rows), 2)
            self.assertEqual({r['id'] for r in rows}, {3})
            self.assertTrue(all(r['voice_transcripts'][0]['status'] == 'complete' for r in rows))
        rows, _ = self.run_query(['message','wechat:message:3'])
        self.assertEqual(rows[0]['media'][0]['voice_transcripts'][0]['text'], '合成语音 English %_!')
        rows, _ = self.run_query(['search','合成语音','--scope','voice','--account','fixture_b'])
        self.assertEqual(rows, [])
        db = self.databases['wechat']
        with admin.connect(db) as c:
            admin.execute(c,"UPDATE media_items SET file_sha256=%s WHERE message_pk=3",('e'*64,));c.commit()
        try:
            rows, _ = self.run_query(['search','合成语音','--scope','voice'])
            self.assertEqual([r['platform'] for r in rows], ['wecom'])
        finally:
            with admin.connect(db) as c:
                admin.execute(c,"UPDATE media_items SET file_sha256=%s WHERE message_pk=3",('c'*64,));c.commit()

    def test_cursor_invalid_or_reused_for_different_filter(self):
        _, page = self.run_query(['messages','--limit','2'])
        for cursor, extra in [('bad', []), (page['next_cursor'], ['--account','fixture_a'])]:
            with self.assertRaises(QueryError):
                self.reader(['messages','--cursor',cursor] + extra)

    def test_partial_failure_and_timeout(self):
        def unavailable(config, db):
            if db == self.databases['wecom']:
                raise pymysql.err.OperationalError(2003, 'sensitive')
            return connect(config, db)
        reader = self.reader(['messages','--limit','2'], unavailable)
        rows, page = reader.run()
        self.assertEqual(len(rows), 2)
        self.assertEqual(reader.errors[0]['platform'], 'wecom')
        self.assertIsNone(page['next_cursor'])
        self.assertTrue(reader.warnings)
        reader = self.reader(['messages'])
        with patch('chatlog_core.query.select', side_effect=pymysql.err.OperationalError(3024,'secret')):
            rows, _ = reader.run()
        self.assertFalse(rows)
        self.assertTrue(all(e['code'] == 'query_timeout' for e in reader.errors))

    def test_json_partial_and_total_failure_exit_codes(self):
        for all_failed in (False, True):
            def unavailable(config, db):
                if all_failed or db == self.databases['wecom']:
                    raise pymysql.err.OperationalError(2003, 'sensitive database detail')
                return connect(config, db)
            reader = self.reader(['messages','--limit','2'], unavailable)
            with patch('chatlog_core.cli.load_config', return_value={}), patch('chatlog_core.cli.Reader', return_value=reader):
                with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                    code = main(['messages','--limit','2','--json'])
            result = json.loads(out.getvalue())
            self.assertEqual(code, 4 if all_failed else 5)
            self.assertEqual(result['partial'], not all_failed)
            self.assertFalse(result['ok'])
            self.assertNotIn('sensitive', out.getvalue() + err.getvalue())

    def test_stale_status(self):
        directory = Path(self.temp.name) / '.runtime/chat-watch-sync'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'wechat.json').write_text(json.dumps(dict(enabled=True,ready=True,mode='direct',heartbeat_unix=1)))
        reader = self.reader(['status'])
        rows, _ = reader.run()
        self.assertTrue(reader.warnings)
        self.assertFalse(rows[0]['sync']['realtime_active'])
        self.assertEqual(rows[0]['history_coverage'], 'unknown')

    def test_mysql_itself_denies_writes_even_without_readonly_session(self):
        reader = self.reader(['messages','--platform','wechat'])
        c = reader.connections['wechat']
        with c.cursor() as cur:
            cur.execute('SELECT @@session.transaction_read_only AS ro')
            self.assertEqual(cur.fetchone()['ro'], 1)
            cur.execute('SET SESSION TRANSACTION READ WRITE')
            for sql in ('UPDATE messages SET text=text WHERE id=0', 'DELETE FROM messages WHERE id=0', 'CREATE TABLE forbidden(id INT)', 'CREATE TEMPORARY TABLE forbidden(id INT)', 'SELECT * FROM mysql.user'):
                with self.assertRaises(pymysql.MySQLError):
                    cur.execute(sql)


if __name__ == '__main__':
    unittest.main()
