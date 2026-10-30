"""Public CE-01 offline contract gate; no CE runtime is implemented here."""

from __future__ import annotations

import unittest
from pathlib import Path

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(
        str(Path(__file__).resolve().parent), pattern="test_*.py"
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
