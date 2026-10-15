#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zero-Leak 扫描器回归测试：既要能抓到真实形态的密钥，也要保证仓库本身零命中。"""

import os
import sys
import tempfile
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))
import leak_scan  # noqa: E402


class TestLeakScan(unittest.TestCase):
    def test_detects_api_key_shapes(self):
        # 运行时拼接，避免本测试文件自身触发扫描
        samples = {
            "OpenAI-compatible": "sk-" + "fWTp" + "A1b2C3d4" * 4,
            "AWS": "AKIA" + "ABCDEFGHIJKLMNOP",
            "GitHub": "ghp" + "_" + "a" * 36,
            "Private key": "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----",
        }
        for label, secret in samples.items():
            hits = leak_scan.scan_text(f'config = {{"apiKey": "{secret}"}}')
            self.assertTrue(hits, f"{label} 形态的密钥必须被识别")
            # 输出必须掩码，避免 CI 日志二次泄露
            for _, sample in hits:
                for s in sample:
                    self.assertNotEqual(s, secret)

    def test_router_placeholder_key_is_not_flagged(self):
        self.assertEqual(leak_scan.scan_text('"apiKey": "sk-workbuddy-router-local"'), [])

    def test_repository_tree_is_clean(self):
        leaks = leak_scan.scan_tree(REPO_DIR)
        self.assertEqual(leaks, [], f"仓库中存在疑似泄露: {leaks}")

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(leak_scan.main([d]), 0)
            with open(os.path.join(d, "bad.py"), "w", encoding="utf-8") as f:
                f.write("KEY = '" + "sk-" + "Z" * 30 + "'\n")
            self.assertEqual(leak_scan.main([d]), 1)


if __name__ == "__main__":
    unittest.main()
