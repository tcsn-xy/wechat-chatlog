"""Synthetic process discovery for official clients and the local signed copy."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from wecom.access import SIGNED_APP, node_processes


class NodeDiscoveryTests(unittest.TestCase):
    def test_official_and_signed_copy_preserve_inspector_discovery(self):
        output = '\n'.join([
            '101 /Applications/企业微信.app/Contents/Resources/WeDrive/v14.17.4/bin/node /Applications/企业微信.app/Contents/Resources/WeDrive/res/node.js',
            f'102 {SIGNED_APP}/Contents/Resources/WeDrive/v14.17.4/bin/node {SIGNED_APP}/Contents/Resources/WeDrive/res/node.js',
            f'103 {SIGNED_APP}/Contents/Resources/wemail_resource/node {SIGNED_APP}/Contents/Resources/wemail_resource/wemail_node/main.js',
            '104 /tmp/other.app/WeDrive/res/node.js',
            '105 /Applications/WeCom.app/Contents/Resources/wemail_node/main.js',
            '106 /tmp/WeComSigned.app/Contents/Resources/WeDrive/res/node.js',
        ])
        with patch('wecom.access.subprocess.run', return_value=Mock(stdout=output)):
            rows = node_processes()
        self.assertEqual([row[0] for row in rows], [101, 102, 103, 105])


if __name__ == '__main__':
    unittest.main()
