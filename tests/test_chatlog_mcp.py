"""MCP protocol and adapter tests: no private data or credentials loaded."""
import contextlib
import io
import json
from pathlib import Path
import selectors
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from chatlog_core.mcp_server import DEFINITIONS, TOOLS, invoke
from chatlog_core.common import QueryError
from install_chatlog_mcp import patch_harness, codex_config_text, BEGIN, END


class AdapterTests(unittest.TestCase):
    def test_readonly_tools_and_schema(self):
        self.assertEqual(len(TOOLS), 9)
        for tool in TOOLS:
            wire = tool.model_dump(by_alias=True)
            self.assertTrue(wire['annotations']['readOnlyHint'])
            self.assertFalse(wire['annotations']['destructiveHint'])
            self.assertFalse(wire['annotations']['openWorldHint'])
            self.assertFalse(wire['inputSchema']['additionalProperties'])
        self.assertEqual(DEFINITIONS['members']['schema']['required'], ['chat'])
        self.assertEqual(DEFINITIONS['search']['schema']['required'], ['keyword'])

    def test_bad_or_write_arguments_do_not_reach_queries(self):
        with patch('chatlog_core.mcp_server.execute', side_effect=AssertionError('must not execute')):
            for name, args in [('delete',{}), ('messages',{'limit':201}), ('messages',{'limit':True}),
                               ('messages',{'sql':'DELETE FROM messages'}), ('search',{}),
                               ('message',{'reference':'wechat:message:1','raw':'yes'}), ('status',None)]:
                result = invoke(name,args)
                self.assertTrue(result.is_error)

    def test_option_like_text_cannot_add_flags(self):
        good = dict(ok=True,partial=False,data=[],errors=[],warnings=[],pagination=None,schema_version=1)
        with patch('chatlog_core.mcp_server.execute', return_value=(good,0)) as run:
            result = invoke('search',{'keyword':'--raw','chat':'--platform=wecom','platform':'wechat'})
            self.assertFalse(result.is_error)
            self.assertEqual(run.call_args.args[0], ['search','--chat=--platform=wecom','--platform=wechat','--','--raw'])

    def test_partial_failure_preserves_structured_result(self):
        partial = dict(ok=False,partial=True,data=[{'platform':'wechat'}],errors=[{'code':'query_timeout'}],warnings=[],pagination=None,schema_version=1)
        with patch('chatlog_core.mcp_server.execute', return_value=(partial,5)):
            result = invoke('messages',{})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content, partial)
        self.assertEqual(json.loads(result.content[0].text), partial)

    def test_capabilities_silent_and_no_config(self):
        with patch('chatlog_core.cli.load_config', side_effect=AssertionError('must not read secrets')):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                result = invoke('capabilities',{})
        self.assertFalse(result.is_error)
        self.assertEqual(out.getvalue(), '')

    def test_harness_patch_idempotent_and_preserves_other_config(self):
        original = '# existing\n- id: existing\n  config: {enabled: true}\n'
        result = patch_harness(original)
        self.assertTrue(result.startswith(original))
        self.assertEqual(result, patch_harness(result))
        self.assertEqual(result.count(BEGIN), 1)
        for bad in ('- id: mcp-chatlog\n', BEGIN, END, END+'\n'+BEGIN):
            with self.assertRaises(QueryError):
                patch_harness(bad)

    def test_codex_merge_preserves_empty_args_and_comments(self):
        original = '# keep comment\n[mcp_servers.other]\ncommand = "test"\nargs = []\n'
        updated = codex_config_text(original)
        self.assertTrue(updated.startswith(original))
        self.assertIn('args = []', updated)


class ProtocolTests(unittest.TestCase):
    def test_legacy_stdio_initialize_discovery_and_tool_call(self):
        # Codex-compatible legacy negotiation as well as the current Python client
        # (separately smoke-tested against both real databases).
        proc = subprocess.Popen([sys.executable,str(ROOT/'scripts/chatlog_mcp.py')],cwd='/tmp',
                                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout,selectors.EVENT_READ)
        def request(i, method, params):
            proc.stdin.write(json.dumps(dict(jsonrpc='2.0',id=i,method=method,params=params))+'\n')
            proc.stdin.flush()
            self.assertTrue(selector.select(10), 'MCP server response timed out')
            response = json.loads(proc.stdout.readline())
            self.assertEqual(response['id'], i)
            return response
        try:
            response = request(1,'initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='chatlog-test',version='1')))
            self.assertEqual(response['result']['protocolVersion'], '2025-11-25')
            proc.stdin.write(json.dumps(dict(jsonrpc='2.0',method='notifications/initialized'))+'\n');proc.stdin.flush()
            response = request(2,'tools/list',{})
            self.assertEqual(len(response['result']['tools']),9)
            response = request(3,'tools/call',dict(name='capabilities',arguments={}))
            self.assertFalse(response['result']['isError'])
            self.assertTrue(response['result']['structuredContent']['ok'])
            response = request(4,'tools/call',dict(name='messages',arguments={'limit':201}))
            self.assertTrue('error' in response or response['result'].get('isError'))
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait()
            selector.close();proc.stdout.close();proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
