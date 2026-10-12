"""Check local documentation navigation and optional authoring source revisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".git", ".venv", "__pycache__", "build", "dist", "test-results"}
LINK = re.compile(r"\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)")


def anchors(path: Path) -> set[str]:
    content = path.read_text(encoding="utf-8")
    result = set(re.findall(r"<a\s+id=[\"\']([^\"\']+)[\"\']", content))
    counts: Counter[str] = Counter()
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", content, re.MULTILINE):
        title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title).lower()
        slug = "".join(
            char for char in title if char in "_- " or unicodedata.category(char)[0] in "LN"
        ).replace(" ", "-")
        count = counts[slug]
        counts[slug] += 1
        result.add(f"{slug}-{count}" if count else slug)
    return result


def check_links(root: Path, boundary: Path) -> tuple[int, list[str]]:
    errors: list[str] = []
    checked = 0
    for path in sorted(root.rglob("*.md")):
        if any(part in IGNORED for part in path.relative_to(root).parts):
            continue
        for match in LINK.finditer(path.read_text(encoding="utf-8")):
            url = urlsplit(match[1].strip("<>"))
            if url.scheme or url.netloc:
                continue
            checked += 1
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            location = f"{path.relative_to(root)}: {match[1]}"
            if not target.is_relative_to(boundary):
                errors.append(f"{location}: escapes source tree")
            elif not target.exists():
                errors.append(f"{location}: missing target")
            elif (
                url.fragment
                and target.suffix == ".md"
                and unquote(url.fragment) not in anchors(target)
            ):
                errors.append(f"{location}: missing fragment")
    return checked, errors


def check_sources(plan_dir: Path) -> tuple[int, list[str]]:
    manifest = json.loads((ROOT / "docs/contracts-sources.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    for name, expected in manifest["sources"].items():
        path = (plan_dir / name).resolve()
        if not path.is_relative_to(plan_dir) or not path.is_file():
            errors.append(f"source {name}: missing or outside plan directory")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(
                f"source {name}: changed since review; reconcile docs/source/contracts.md"
            )
    return len(manifest["sources"]), errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan-dir", type=Path, help="optional authoritative observability-plan directory"
    )
    args = parser.parse_args()
    checked, errors = check_links(ROOT, ROOT)
    sources = 0
    if args.plan_dir is not None:
        sources, source_errors = check_sources(args.plan_dir.resolve())
        errors.extend(source_errors)
    for error in errors:
        print(error, file=sys.stderr)
    print(
        json.dumps(
            {
                "ok": not errors,
                "local_links": checked,
                "reviewed_sources": sources,
                "errors": len(errors),
            },
            sort_keys=True,
        )
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
