# Sledra local regression

Run from the repository root after [dependency preparation](quickstart.md#prepare-dependencies). The following commands use existing synthetic fixtures and isolated temporary directories. Offline Go builds need a populated module cache.

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python spec/verify.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python spec/verify_event.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python spec/verify_context.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/shell/verify.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/m1/verify.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/m1/replay.py tests/m1/results/handoff.json
(cd recorder && GOPROXY=off GOSUMDB=off go test ./...)
python3 scripts/check_docs.py
```

An existing Python environment with `jsonschema` may use `python3` in place of `.venv/bin/python`. Contract checks compare Go/Python bytes, identity vectors and positive/negative cases. Shell checks exercise disabled/enabled behavior and failure isolation. M1 checks cover normal execution, failure/retry and the frozen handoff. Keep `tests/m1/results/` and `tests/platform/results/` unchanged during ordinary regressions.

The [M1 guide](../tests/m1/README.md) documents deliberate fixture refresh and a separate timing probe. The [platform probe](../tests/platform/README.md) requires a writable test parent and establishes only local filesystem primitives. Save any new probe report to a separate location rather than overwriting a historical report.

`scripts/check_docs.py` validates repository Markdown links, fragments and confinement to the source tree. For an authoring workspace, add `--plan-dir /absolute/path/to/docs/observability-plan` to compare reviewed authoritative source digests. An independent source copy can perform the local checks without that optional directory. See [contract sources](contracts.md) for the review workflow.
