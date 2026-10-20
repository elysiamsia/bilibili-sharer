# -*- coding: utf-8 -*-
"""仓库内自检的总入口：`python tests/run_all.py`（CI 也用它）。

刻意只用标准库：不 import Bot 框架、不联网、不读写插件运行目录，
任何一台装了 Python 3.11+ 的机器上都能跑。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    sys.path.insert(0, str(HERE))
    loader = unittest.TestLoader()
    suite = loader.discover(str(HERE), pattern="test_*.py", top_level_dir=str(HERE))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
