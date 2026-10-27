"""Shared P0 gate: contract vectors, schema consistency and mock interface examples."""

from __future__ import annotations

import unittest
from pathlib import Path


def main() -> int:
    directory = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(directory), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
