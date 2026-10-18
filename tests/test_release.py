"""Release manifest and privacy scanner policy, using synthetic values only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import fixtures  # establishes library and script search paths

import privacy_scan
import release


class PathPolicy(unittest.TestCase):
    def test_public_paths_allowed(self):
        for name in ('lib/local_config.py', 'scripts/release.py', 'docs/reading.md',
                     '.github/workflows/ci.yml', '.gitignore', '使用说明.md'):
            self.assertTrue(privacy_scan.allowed_path(name), name)

    def test_private_paths_rejected(self):
        for name in ('keys/local.json', 'data/x.db', 'export/index.html', 'media_store/a.jpg',
                     'models/x.bin', '.venv/bin/python', '.runtime/x.json', 'docs/private/x.md',
                     'experiments/wechat_sender/sender.py', 'a/../b', '/etc/passwd',
                     'docs/x.db', 'x.zip', '.env.local'):
            self.assertFalse(privacy_scan.allowed_path(name), name)

    def test_local_only_release_paths_rejected(self):
        for name in ('AGENTS.local.md', 'docs/private/x.md', 'experiments/a.py',
                     'scripts/assemble_profiles.py', 'native/wecom_probe.entitlements',
                     'native/wecom_probe.c'):
            self.assertTrue(release.is_local_only(name), name)


class Scanner(unittest.TestCase):
    def scan(self, text, deny=()):
        return privacy_scan.scan_blob('synthetic.txt', text.encode(), [item.encode() for item in deny])

    def categories(self, text, deny=()):
        return {item['category'] for item in self.scan(text, deny)}

    def test_detects_tokens_and_identifiers(self):
        self.assertIn('github_token', self.categories('ghp_' + 'a' * 30))
        self.assertIn('api_token', self.categories('sk-' + 'b' * 30))
        self.assertIn('private_key', self.categories('-----BEGIN ' + 'RSA PRIVATE KEY-----'))
        self.assertIn('personal_home', self.categories('/Users/' + 'synthetic/Library/x'))
        self.assertIn('wechat_id', self.categories('wxid_' + 'abcdefghijklm'))
        self.assertIn('personal_chat_id', self.categories('9876543' + '2101@chatroom'))
        self.assertIn('enterprise_user_id', self.categories('1688' + '123456789012'))
        self.assertIn('openim_id', self.categories('123456789' + '@openim'))
        self.assertIn('qq_email', self.categories('12345678' + '@qq.com'))
        self.assertIn('cn_mobile', self.categories('138' + '00138000'))

    def test_placeholders_and_synthetic_ids_are_allowed(self):
        self.assertEqual(self.scan('10000000001@chatroom'), [])
        self.assertEqual(self.scan('10000000002@chatroom'), [])
        self.assertEqual(self.scan('wxid_example'), [])
        self.assertEqual(self.scan('wxid_self'), [])
        self.assertEqual(self.scan('/Users/example/project'), [])

    def test_deny_file_values_are_reported_without_values(self):
        findings = self.scan('contains synthetic-secret-marker', deny=['synthetic-secret-marker'])
        self.assertEqual([item['category'] for item in findings], ['private_denylist'])
        self.assertNotIn('synthetic-secret-marker', json.dumps(findings))

    def test_manifest_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ['.gitignore', 'lib/x.py', 'release-manifest.json']
            for name in names:
                if name == 'release-manifest.json':
                    continue
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('data')
            (root / 'release-manifest.json').write_text(json.dumps(names))
            self.assertEqual(privacy_scan.manifest_files(root), names)


if __name__ == '__main__':
    unittest.main()
