#!/usr/bin/env python
"""隔离模式启动器：-I 不把 cwd 加入 sys.path，这里显式加入项目根。

用法（从任何目录均可）：
    python -I run_querymart.py query
    python -I run_querymart.py reconcile --query sum-budget
    python -I run_querymart.py compare --query distinct-cross-days
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from querymart.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
