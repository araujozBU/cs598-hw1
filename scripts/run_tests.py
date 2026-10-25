"""Run offline tests, or all live tests with spacing on the shared endpoint."""

import argparse
import os
from pathlib import Path
import sys
import time
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Enable every endpoint test.')
    parser.add_argument('--pause', type=float, default=10, help='Seconds between live agent invocations (default 10).')
    args = parser.parse_args()
    if args.pause < 0:
        parser.error('--pause must not be negative')
    flags = ['RUN_LIVE_TESTS', 'RUN_EXTENDED_LIVE_TESTS', 'RUN_CHECKPOINT2_LIVE_TESTS', 'RUN_FINAL_LIVE_TESTS']
    for flag in flags:
        if args.live:
            os.environ[flag] = '1'
        else:
            os.environ.pop(flag, None)
    if args.live and not os.environ.get('QWEN_API_KEY'):
        parser.error('Set QWEN_API_KEY before running live tests.')
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'tests'))
    if args.live:
        from test_agent import LiveTests, ExtendedLiveTests
        from test_csv import Checkpoint2LiveTests
        from test_memory_regressions import MemoryLiveTests
        from test_final_live import FinalLiveTests
        def paced(function):
            def invoke(self, *a, **kw):
                time.sleep(args.pause)
                return function(self, *a, **kw)
            return invoke
        for cls, method in [(LiveTests, 'run_task'), (ExtendedLiveTests, 'run_task'),
                            (Checkpoint2LiveTests, 'turn'), (MemoryLiveTests, 'turn'), (FinalLiveTests, 'invoke')]:
            setattr(cls, method, paced(getattr(cls, method)))
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
