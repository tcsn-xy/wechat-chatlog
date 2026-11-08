"""Synthetic-only semantic search, durable indexing and provider-boundary tests."""
from __future__ import annotations

import contextlib
import io
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from chatlog_core.common import QueryError
from chatlog_core.cli import execute, parser
from chatlog_core import semantic as sem
from chatlog_core.semantic_index import Indexer
from chatlog_core.query import Reader


def vector(n=0):
    result = [0.0]*sem.DIMENSIONS
    result[n] = 1.0
    return result


def row(pk=1, text='这个岗位每个月工资两万元。', platform='wechat', account='synthetic-account', **extra):
    value = dict(id=pk, platform=platform, account_key=account, chat_pk=10, chat_id='synthetic-chat',
                 ts=1700000000, kind='text', sender_id='synthetic-sender', sender_name='合成联系人',
                 text=text, source_dataset='synthetic', source_db='message.db', source_table='Msg_synthetic',
                 local_id=pk, server_id=str(pk), has_media=0, has_link=0)
    value.update(extra)
    return value


class FakeEmbedder:
    def __init__(self):
        self.calls = []
        self.fail_at = None

    def embed(self, texts, purpose):
        if self.fail_at == len(self.calls):
            raise sem.YieldBackground()
        self.calls.append((list(texts), purpose))
        return [vector(1 if '公园' in s else 0) for s in texts]

    def query_waiting(self):
        return False


@unittest.skipUnless(importlib.util.find_spec('lancedb'), 'optional requirements-semantic.txt not installed')
class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'state'
        self.store = sem.Store(self.root, create=True)
        self.config = dict(endpoint=sem.ENDPOINT, model=sem.MODEL, dimensions=sem.DIMENSIONS,
                           api_key='synthetic', text_external_consent=True, scope='all_history_and_new')
        self.config_path = Path(self.tmp.name)/'config.json'
        self.config_path.write_text(json.dumps(self.config)); self.config_path.chmod(0o600)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def platform(self, p='wechat', total=10, done=False):
        self.store.db.execute('INSERT OR REPLACE INTO platforms(platform,target,back_cursor,live_cursor,total,back_done) '
                              'VALUES (?,?,?,?,?,?)', (p, 100, 101, 100, total, int(done)))
        self.store.db.commit()

    def indexer(self):
        embedder = FakeEmbedder()
        index = sem.VectorIndex(self.root, create=True)
        return Indexer(self.store, self.config, database_config={'synthetic': True}, index=index, embedder=embedder)

    def reader(self, rows, *argv):
        args = parser().parse_args(['semantic_search', '报酬是多少', *argv])
        reader = Reader.__new__(Reader)
        reader.args, reader.chat, reader.errors, reader.warnings = args, None, [], []
        reader.platforms = ['wechat', 'wecom'] if args.platform == 'all' else [args.platform]
        reader.connections = {p: object() for p in reader.platforms}
        def resolve():
            if args.chat:
                p, pk = sem.parse_ref(args.chat, 'chat')
                if p not in reader.platforms:
                    raise QueryError('platform_mismatch', '合成平台冲突')
                found = [r for r in rows if r['platform'] == p and r['chat_pk'] == pk
                         and (not args.account or r['account_key'] == args.account)]
                if not found:
                    raise QueryError('chat_not_found', '合成目标未找到')
                reader.chat = dict(platform=p, chat_pk=pk, account_key=found[0]['account_key'])
        reader.resolve_chat = resolve
        return reader

    def search(self, reader, rows):
        with patch('chatlog_core.query.select', return_value=rows), patch.object(sem.Embedder, 'embed', return_value=[vector()]):
            return sem.search(reader, self.config_path, self.root)

    def test_config_refuses_unsafe_permissions_symlinks_and_missing_consent(self):
        self.assertEqual(sem.settings(self.config_path)['model'], 'embo-01')
        self.config_path.chmod(0o644)
        with self.assertRaises(QueryError):sem.settings(self.config_path)
        self.config_path.chmod(0o600)
        link = Path(self.tmp.name)/'link'; link.symlink_to(self.config_path)
        with self.assertRaises(QueryError):sem.settings(link)
        self.config['text_external_consent'] = False
        self.config_path.write_text(json.dumps(self.config))
        with self.assertRaises(QueryError):sem.settings(self.config_path)

    def test_unicode_chunk_bounds_and_overlap_preserve_text(self):
        text = '中文🙂工资 20000 不是 2000\n'*400
        parts = list(sem.chunks(text))
        self.assertEqual(parts[0][0], 0); self.assertEqual(parts[-1][1], len(text))
        for i, (start, end, chunk) in enumerate(parts):
            self.assertEqual(chunk, text[start:end]); self.assertLessEqual(len(chunk.encode()), 2400)
            if i:self.assertLess(start, parts[i-1][1])
        self.assertEqual(''.join(chunk if not i else chunk[parts[i-1][1]-start:]
                                 for i, (start, end, chunk) in enumerate(parts)), text)

    def test_system_raw_xml_and_media_placeholders_are_skipped(self):
        for text in ('', '  ', '[图片]', '[语音]', '[图片]（本机无缓存）', '<msg><appmsg>合成</appmsg></msg>'):
            self.assertEqual(sem.text_of(row(text=text)), '')
        self.assertEqual(sem.text_of(row(kind='system')), '')
        self.assertIn('不是', sem.text_of(row(text='不是两千，是两万元')))
        self.assertEqual(sem.text_of(row(text='[链接] 工资方案')), '[链接] 工资方案')

    def test_provider_rejects_wrong_count_dimension_nan_bool_and_zero(self):
        for vs in ([], [[1.0]], [[0.0]*1536], [[float('nan')]*1536], [[True]*1536], [[float('inf')]*1536]):
            with self.assertRaises(QueryError):sem.validate_vectors({'vectors': vs}, 1)
        self.assertEqual(sem.validate_vectors({'vectors': [vector()]}, 1), [vector()])

    def test_repeated_text_reuses_embedding_but_preserves_each_source(self):
        worker = self.indexer()
        worker.process([row(1), row(2, platform='wecom', account='other-account')])
        self.assertEqual(len(worker.embedder.calls), 1)
        self.assertEqual(len(worker.embedder.calls[0][0]), 1)
        worker.process([row(3)])
        self.assertEqual(len(worker.embedder.calls), 1)
        self.assertEqual(worker.index.table.count_rows(), 3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 3)

    def test_idempotent_source_and_changed_text_update(self):
        worker = self.indexer()
        original = row()
        worker.process([original]); worker.process([original])
        self.assertEqual(worker.index.table.count_rows(), 1)
        worker.process([row(text='今天去公园。')])
        self.assertEqual(worker.index.table.count_rows(), 1)
        self.assertEqual(len(worker.embedder.calls), 2)
        self.assertEqual(original['text'], '这个岗位每个月工资两万元。')

    def test_pending_vectors_survive_priority_yield_and_resume(self):
        worker = self.indexer()
        data = [row(1, '中'*700), row(2, '文'*700)]
        worker.embedder.fail_at = 1
        with self.assertRaises(sem.YieldBackground):worker.process(data)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)
        worker.embedder.fail_at = None
        worker.process(data)
        self.assertEqual(len(worker.embedder.calls), 2)
        self.assertEqual(worker.index.table.count_rows(), 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0], 0)

    def test_checkpoint_never_precedes_vector_commit(self):
        worker = self.indexer()
        with patch.object(worker.index, 'upsert', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):worker.process([row()])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)
        worker.process([row()])
        self.assertEqual(len(worker.embedder.calls), 1)
        self.assertEqual(worker.index.table.count_rows(), 1)

    def test_semantic_ranking_and_partial_coverage(self):
        self.platform()
        worker = self.indexer()
        rows = [row(), row(2, '今天去公园。')]
        worker.process(rows)
        reader = self.reader(rows, '--platform', 'wechat')
        data, page = self.search(reader, rows)
        self.assertEqual(data[0]['message_ref'], 'wechat:message:1')
        self.assertGreater(data[0]['similarity'], data[1]['similarity'])
        self.assertEqual(data[0]['matched_text'], rows[0]['text'])
        self.assertFalse(page['coverage']['history_scan_complete'])
        self.assertTrue(any('尚未覆盖' in w for w in reader.warnings))

    def test_actual_index_prefilters_platform_account_chat_sender_and_time(self):
        self.platform(); self.platform('wecom')
        worker = self.indexer()
        rows = [row(), row(2, account='another-account'), row(3, platform='wecom'),
                row(4, chat_pk=11), row(5, sender_id='different'), row(6, ts=1)]
        worker.process(rows)
        reader = self.reader(rows, '--platform', 'wechat', '--account', 'synthetic-account',
                             '--chat', 'wechat:chat:10', '--sender', 'synthetic-sender', '--since', '2020-01-01')
        data, _ = self.search(reader, rows)
        self.assertEqual([r['id'] for r in data], [1])

    def test_query_rehydrates_current_text_and_drops_stale_or_deleted_rows(self):
        self.platform()
        worker = self.indexer(); worker.process([row(), row(2)])
        rows = [row(text='原文已修改')]
        reader = self.reader(rows, '--platform', 'wechat')
        data, _ = self.search(reader, rows)
        self.assertEqual(data, [])
        self.assertTrue(any('失效' in w for w in reader.warnings))

    def test_long_message_chunks_collapse_to_one_result(self):
        self.platform()
        worker = self.indexer(); rows = [row(text='工资讨论🙂'*800)]
        worker.process(rows)
        data, _ = self.search(self.reader(rows, '--platform', 'wechat'), rows)
        self.assertEqual(len(data), 1)
        self.assertLessEqual(len(data[0]['matched_text']), 600)

    def test_exact_chat_only_and_platform_conflict_before_provider_call(self):
        for argv in (['--chat', '合成昵称'], ['--platform', 'wecom', '--chat', 'wechat:chat:10']):
            with patch.object(sem.Embedder, 'embed', side_effect=AssertionError('must not upload')):
                with self.assertRaises(QueryError):sem.search(self.reader([row()], *argv), self.config_path, self.root)

    def test_account_mismatch_rejected_before_provider_call(self):
        reader = self.reader([row()], '--chat', 'wechat:chat:10', '--account', 'wrong-account')
        with patch.object(sem.Embedder, 'embed', side_effect=AssertionError('must not upload')):
            with self.assertRaises(QueryError):sem.search(reader, self.config_path, self.root)

    def test_provider_failure_never_falls_back_to_like(self):
        self.platform(); worker = self.indexer(); worker.process([row()])
        with patch.object(sem.Embedder, 'embed', side_effect=QueryError('semantic_network_error', '合成超时')):
            with self.assertRaises(QueryError) as ctx:
                sem.search(self.reader([row()]), self.config_path, self.root)
        self.assertEqual(ctx.exception.detail['code'], 'semantic_network_error')

    def test_cli_and_mcp_share_results_and_annotations(self):
        from chatlog_core.mcp_server import invoke, DEFINITIONS, TOOLS
        expected = ([dict(message_ref='wechat:message:1', similarity=.8)], dict(order='relevance', coverage={}))
        reader = self.reader([row()]); reader.close = lambda: None
        with patch('chatlog_core.cli.Reader', return_value=reader), patch('chatlog_core.cli.load_config', return_value={}), \
             patch.object(sem, 'search', return_value=expected):
            cli, code, _ = execute(['semantic_search', '报酬'])
            mcp = invoke('semantic_search', {'query': '报酬'})
        self.assertEqual(code, 0); self.assertEqual(cli, mcp.structured_content)
        self.assertEqual(DEFINITIONS['semantic_search']['schema']['properties']['limit']['maximum'], 100)
        self.assertTrue(next(t for t in TOOLS if t.name=='semantic_search').annotations.open_world_hint)
        self.assertTrue(next(t for t in TOOLS if t.name=='semantic_search').annotations.read_only_hint)

    def test_keywords_stay_literal_and_semantic_limits_are_separate(self):
        self.assertEqual(parser().parse_args(['search', '工资']).command, 'search')
        self.assertEqual(parser().parse_args(['semantic_search', '报酬']).limit, 20)
        for argv in (['semantic_search', '报酬', '--limit', '101'], ['semantic_search', '报酬', '--cursor', 'x']):
            result, code, _ = execute(argv)
            self.assertEqual(code, 2); self.assertFalse(result['ok'])

    def test_lancedb_filter_escaping_treats_sql_as_data(self):
        self.platform(); worker = self.indexer()
        rows = [row(sender_id="person' OR 1=1 --"), row(2)]
        worker.process(rows)
        data, _ = self.search(self.reader(rows, '--platform', 'wechat', '--sender', "person' OR 1=1 --"), rows)
        self.assertEqual([r['id'] for r in data], [1])

    def test_provider_payload_and_redacted_business_errors(self):
        class Opener:
            def open(_, request, timeout):
                self.assertEqual(request.full_url, sem.ENDPOINT)
                body = json.loads(request.data)
                self.assertEqual(set(body), {'model', 'texts', 'type'})
                return io.BytesIO(json.dumps(dict(base_resp=dict(status_code=1008,
                                    status_msg='private-key-or-chat'), vectors=[])).encode())
        embedder = sem.Embedder(self.config, self.store, Opener())
        with self.assertRaises(QueryError) as ctx:embedder.embed(['合成问题'], 'query')
        self.assertEqual(ctx.exception.detail['code'], 'semantic_quota')
        self.assertNotIn('private-key', str(ctx.exception))
        self.assertNotIn('合成问题', str(list(self.store.db.execute('SELECT * FROM requests'))))

    def test_paused_background_and_query_priority_yield_before_upload(self):
        embedder = sem.Embedder(self.config, self.store)
        self.store.set('paused', True)
        with self.assertRaises(sem.YieldBackground):embedder.embed(['合成'], 'db')
        self.store.set('paused', False)
        with embedder.priority():
            self.assertTrue(embedder.query_waiting())
            with self.assertRaises(sem.YieldBackground):embedder.embed(['合成'], 'db')
        self.assertFalse(list(self.root.glob('query-*.request')))

    def test_restart_retains_history_live_and_reconciliation_cursors(self):
        self.platform()
        self.store.db.execute('UPDATE platforms SET back_cursor=41,live_cursor=110,reconcile_cursor=21,reconcile_target=100')
        self.store.db.commit()
        other = sem.Store(self.root)
        try:
            state = other.db.execute('SELECT * FROM platforms').fetchone()
            self.assertEqual((state['back_cursor'], state['live_cursor'], state['reconcile_cursor']), (41,110,21))
        finally:other.close()

    def test_live_and_history_cursors_advance_only_after_processing(self):
        self.platform(); worker = self.indexer()
        with patch.object(worker, 'rows', return_value=[row(101)]), \
             patch.object(worker, 'process', side_effect=sem.YieldBackground):
            with self.assertRaises(sem.YieldBackground):worker.step_live('wechat')
        self.assertEqual(self.store.db.execute('SELECT live_cursor FROM platforms').fetchone()[0], 100)
        with patch.object(worker, 'rows', return_value=[row(101)]):worker.step_live('wechat')
        self.assertEqual(self.store.db.execute('SELECT live_cursor FROM platforms').fetchone()[0], 101)

    def test_query_calls_serialize_and_share_rate_limit_across_clients(self):
        starts, errors = [], []
        class Opener:
            def open(_, request, timeout):
                starts.append(time.monotonic())
                return io.BytesIO(json.dumps(dict(base_resp=dict(status_code=0), vectors=[vector()], total_tokens=1)).encode())
        def query():
            store = sem.Store(self.root)
            try:
                embedder = sem.Embedder(self.config, store, Opener())
                with embedder.priority():embedder.embed(['合成'], 'query')
            except Exception as exc:errors.append(type(exc).__name__)
            finally:store.close()
        threads = [threading.Thread(target=query) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, []); self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1]-starts[0], .95)

    def test_missing_platform_never_reports_full_history_coverage(self):
        self.platform(done=True)
        self.assertFalse(self.store.coverage()['history_scan_complete'])
        self.assertTrue(self.store.coverage(['wechat'])['history_scan_complete'])

    def test_management_pause_resume_persists_without_exposing_mcp_admin(self):
        from chatlog_core import semantic_app
        from chatlog_core.mcp_server import DEFINITIONS
        self.assertNotIn('semantic', DEFINITIONS)
        for action, expected in [('pause', True), ('resume', False)]:
            with patch.object(semantic_app, 'Store', side_effect=lambda **kw: sem.Store(self.root)), \
                 patch.object(semantic_app, 'settings', return_value=self.config), \
                 patch.object(semantic_app, 'status', return_value={}), \
                 patch.object(semantic_app, 'launch') as launch, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(semantic_app.main([action, '--json']), 0)
            self.assertEqual(self.store.get('paused'), expected)
            self.assertEqual(launch.call_count, int(action=='resume'))

    def test_http_timeout_auth_and_redirect_are_redacted(self):
        import urllib.error
        for exc, code in [(TimeoutError('synthetic private body'), 'semantic_network_error'),
                          (urllib.error.HTTPError(sem.ENDPOINT, 401, 'private', {}, None), 'semantic_auth'),
                          (urllib.error.HTTPError(sem.ENDPOINT, 429, 'private', {}, None), 'semantic_rate_limited'),
                          (urllib.error.HTTPError(sem.ENDPOINT, 302, 'private', {}, None), 'semantic_provider_error')]:
            opener = type('Opener', (), {'open': lambda _, *a, **kw: (_ for _ in ()).throw(exc)})()
            self.store.set('next_request', 0)
            with self.assertRaises(QueryError) as ctx:sem.Embedder(self.config, self.store, opener).embed(['合成'], 'query')
            self.assertEqual(ctx.exception.detail['code'], code)
            self.assertNotIn('private', str(ctx.exception))

    def test_initialize_accepts_mysql_unsigned_max_decimal(self):
        from decimal import Decimal
        worker = self.indexer()
        with patch('chatlog_core.semantic_index.select', return_value=[dict(maximum=Decimal(101), total=50)]), \
             patch.object(worker, 'connection', return_value=object()):
            worker.initialize('wechat')
        state = self.store.db.execute('SELECT * FROM platforms').fetchone()
        self.assertEqual(state['live_cursor'], 101)
        self.assertEqual(self.store.get('initial_rows_wechat'), 50)

    def test_real_ivf_pq_recall_and_unindexed_new_chunks(self):
        import numpy as np
        rng = np.random.default_rng(731)
        base = rng.normal(size=(32, sem.DIMENSIONS)).astype('float32')
        base /= np.linalg.norm(base, axis=1)[:, None]
        matrix = base[np.arange(4096)%32] + rng.normal(0,.025,size=(4096,sem.DIMENSIONS)).astype('float32')
        matrix /= np.linalg.norm(matrix, axis=1)[:, None]
        worker = self.indexer()
        def point(i, value):
            return dict(uid=str(i),digest=str(i),hash='synthetic',platform='wechat' if i%2 else 'wecom',
                        account='synthetic',message_pk=i+1,chat_pk=1,sender='',ts=1,start=0,end=1,vector=value.tolist())
        worker.index.upsert([point(i, matrix[i]) for i in range(len(matrix))])
        with __import__('warnings').catch_warnings():
            __import__('warnings').simplefilter('ignore', DeprecationWarning)
            worker.maintain()
        recalls = []
        for i in range(8):
            query = (matrix[200+i]+rng.normal(0,.002,sem.DIMENSIONS)).tolist()
            exact = worker.index.candidates(query, "platform='wechat'", 10, True)
            approx = worker.index.candidates(query, "platform='wechat'", 10)
            recalls.append(len({r['uid'] for r in exact}&{r['uid'] for r in approx})/10)
        self.assertGreaterEqual(sum(recalls)/len(recalls), .9)
        worker.index.upsert([point(4097, matrix[1])])
        hits = worker.index.candidates(matrix[1].tolist(), "platform='wechat'", 3)
        self.assertIn('4097', {r['uid'] for r in hits})

    def test_fast_profile_batches_short_messages_without_changing_embeddings(self):
        self.store.set('performance_profile', 'fast')
        worker = self.indexer()
        data = [row(i+1, ('第%d条合成记录，工资没有改变。'%i)*12) for i in range(64)]
        worker.process(data)
        # One operation still uses the same model/chunks; only batching changes.
        self.assertLessEqual(len(worker.embedder.calls), 4)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 64)
        self.assertEqual(worker.index.table.count_rows(), 64)

    def test_fast_profile_keeps_requests_bounded_and_foreground_limit_unchanged(self):
        self.store.set('performance_profile', 'fast')
        worker = self.indexer()
        worker.process([row(i+1, ('合成记录%d：'%i)*200) for i in range(65)])
        self.assertTrue(all(len(texts)<=64 and sum(len(t.encode()) for t in texts)<=32768
                            for texts, purpose in worker.embedder.calls))
        self.assertTrue(all(purpose=='db' for _,purpose in worker.embedder.calls))
        with self.assertRaises(QueryError):sem.Embedder(self.config,self.store).embed(['中'*1100], 'query')

    def test_cache_hits_are_read_in_bulk_and_keep_distinct_sources(self):
        worker = self.indexer()
        worker.process([row(i+1,'合成文字%d'%i) for i in range(16)])
        with patch.object(worker.index, 'vectors', wraps=worker.index.vectors) as bulk, \
             patch.object(worker.index, 'vector', side_effect=AssertionError('no repeated lookups')):
            worker.process([row(i+101,'合成文字%d'%i) for i in range(16)])
        self.assertEqual(bulk.call_count, 1)
        self.assertEqual(len(worker.embedder.calls), 1)
        self.assertEqual(worker.index.table.count_rows(), 32)

    def test_history_page_byte_bound_does_not_mark_history_complete(self):
        self.store.set('performance_profile','fast');self.platform()
        worker=self.indexer()
        with patch.object(worker,'connection',return_value=object()), \
             patch('chatlog_core.semantic_index.select',return_value=[row(i+1,'中'*21000) for i in range(256)]) as read:
            limited=worker.rows('wechat','m.id<%s',[100])
        self.assertEqual(read.call_args.args[2][-1],256)
        self.assertEqual(len(limited),1)
        with patch.object(worker,'rows',return_value=[row(50,'合成')]):worker.step_history('wechat')
        self.assertFalse(self.store.db.execute('SELECT back_done FROM platforms').fetchone()[0])
        with patch.object(worker,'rows',return_value=[]):worker.step_history('wechat')
        self.assertTrue(self.store.db.execute('SELECT back_done FROM platforms').fetchone()[0])

    def test_pending_vector_binary_cache_reads_existing_json_after_upgrade(self):
        worker=self.indexer();text='合成的升级恢复样本';key=sem.digest(text)
        self.store.db.execute('INSERT INTO pending VALUES (?,?)',(key,json.dumps(vector())))
        self.store.db.commit()
        worker.process([row(1,text)])
        self.assertEqual(worker.embedder.calls,[])
        self.assertEqual(worker.index.table.count_rows(),1)

    def test_speed_control_preserves_pause_and_is_not_mcp_management(self):
        from chatlog_core import semantic_app
        from chatlog_core.mcp_server import DEFINITIONS
        self.store.set('paused',True)
        with patch.object(semantic_app,'Store',side_effect=lambda **kw:sem.Store(self.root)), \
             patch.object(semantic_app,'settings',return_value=self.config), \
             patch.object(semantic_app,'status',return_value={}), \
             patch.object(semantic_app,'launch') as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(semantic_app.main(['speed','--profile','fast','--json']),0)
        self.assertEqual(self.store.get('performance_profile'),'fast')
        self.assertTrue(self.store.get('paused'))
        launch.assert_not_called()
        self.assertNotIn('speed',DEFINITIONS)


if __name__ == '__main__':
    unittest.main()
