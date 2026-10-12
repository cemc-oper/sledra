"""Select a shared development binary or build one for a standalone test."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("sledra", "sledra-spec-check", "sledra-event", "sledra-spool", "sledra-retention")


def offline_env() -> dict[str, str]:
    return dict(
        os.environ,
        GOPROXY="off",
        GOSUMDB="off",
        GONOPROXY="none",
        GOTOOLCHAIN="local",
        UV_OFFLINE="1",
        UV_PYTHON_DOWNLOADS="never",
        PYTHONDONTWRITEBYTECODE="1",
        CGO_ENABLED="0",
    )


def binary_for(name: str, scratch: Path) -> Path:
    """Explicit shared binaries must exist; a missing override never rebuilds."""
    if name not in TOOLS:
        raise ValueError(f"unknown development tool: {name}")
    if directory := os.environ.get("SLEDRA_TEST_BIN_DIR"):
        binary = Path(directory).resolve() / name
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise FileNotFoundError(
                f"missing executable development tool: {binary}; run make build"
            )
        return binary
    binary = scratch / name
    subprocess.run(
        ["go", "build", "-buildvcs=false", "-trimpath", "-o", str(binary), f"./cmd/{name}"],
        cwd=ROOT / "recorder",
        env=offline_env(),
        check=True,
    )
    return binary
