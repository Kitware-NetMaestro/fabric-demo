"""
Collects every offline test suite in the repository for plain
`python3 -m unittest discover` run from the repository root.

The test directories (trace/tests, experiments/tests, analysis/tests) are not
Python packages, and must not become ones: a `trace/__init__.py` would shadow the
standard library's `trace` module. Default discovery does not descend into
non-package directories, so this module's load_tests hook discovers each of them
with itself as the top-level directory, exactly as
`python3 -m unittest discover -s <dir>` does. Test module basenames must
therefore be unique across the directories.

Run from the repository root (either form):
    python3 -m unittest discover -v
    python3 -m unittest discover -s analysis/tests -v      # one directory
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_DIRS = ["trace/tests", "experiments/tests", "analysis/tests"]


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(tests)
    for rel in TEST_DIRS:
        start = str(ROOT / rel)
        suite.addTests(loader.discover(start_dir=start, pattern=pattern or "test*.py",
                                       top_level_dir=start))
    return suite
