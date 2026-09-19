"""Run both test suites.

Usage:  python run_tests.py
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_app  # noqa: E402
import test_core  # noqa: E402
import test_live  # noqa: E402  (self-skips when no endpoint is reachable)


def main() -> int:
    if not test_live.LIVE:
        print("Live endpoint %s unreachable - those tests will skip.\n"
              % test_live.BASE_URL)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromModule(test_core),
        loader.loadTestsFromModule(test_app),
        loader.loadTestsFromModule(test_live),
    ])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
