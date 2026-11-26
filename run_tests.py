"""跑全部离线测试。

    python run_tests.py

不需要 N.E.K.O、不需要 pytest、不需要真的在打游戏。测试只驱动
纯逻辑和插件状态机，用假的 Live Client Data API 喂数据。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# neko_lol 的父目录（让 `import neko_lol` 成立）+ 插件目录（让 `import cues` 之类成立）
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "tests"))

_MODULES = ("tests.test_live_client", "tests.test_offline", "tests.test_session")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in _MODULES:
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except Exception as exc:  # 收集失败也要说清楚是哪个文件
            print(f"!! 无法加载 {name}: {type(exc).__name__}: {exc}")
            return 2

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
