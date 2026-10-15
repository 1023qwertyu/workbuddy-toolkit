#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行单元测试，并把失败 / 错误以 GitHub Actions annotation 的形式输出。

这样失败原因会直接显示在 PR 的 Checks 页面（以及 check-run annotations API），
不必下载完整日志。退出码与 `python -m unittest discover` 一致。

用法: python scripts/ci_test_report.py [tests_dir]
"""

import os
import sys
import unittest

MAX_ANNOTATIONS = 20
MAX_MESSAGE = 3500


def _escape(text):
    # https://docs.github.com/actions/using-workflows/workflow-commands-for-github-actions
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    start_dir = argv[0] if argv else "tests"
    suite = unittest.defaultTestLoader.discover(start_dir)
    result = unittest.TextTestRunner(verbosity=2).run(suite)

    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    problems = [("FAIL", t, tb) for t, tb in result.failures] + [("ERROR", t, tb) for t, tb in result.errors]
    for kind, test, tb in problems[:MAX_ANNOTATIONS]:
        title = f"{kind}: {test.id()}"
        body = tb[-MAX_MESSAGE:]
        if in_actions:
            print(f"::error title={_escape(title)}::{_escape(body)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
