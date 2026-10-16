"""Business-side product check; records a result without claiming delivery."""

import json
import sys
from pathlib import Path

expected_file, product_dir, result_file = map(Path, sys.argv[1:])
expected = json.loads(expected_file.read_text())
product = product_dir / expected["path"]
ok = product.is_file() and product.read_text() == expected["content"]
result_file.write_text(
    json.dumps({"check": "synthetic-content", "result": "pass" if ok else "fail"}) + "\n"
)
if ok:
    print("verify:pass")
else:
    print("verify:fail", file=sys.stderr)
    raise SystemExit(3)
