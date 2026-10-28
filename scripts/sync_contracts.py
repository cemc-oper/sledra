"""Copy canonical generated resources into recorder and analysis resource directories."""

from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def sync_execution(root: Path) -> None:
    """Copy only the two v2 resources for components present in this checkout."""
    for component, directory in (
        ("recorder/execution", "recorder/execution/contract"),
        (
            "analysis/src/sledra_analysis/execution",
            "analysis/src/sledra_analysis/resources/execution_v2",
        ),
    ):
        if (root / component).is_dir():
            target = root / directory / "contract.schema.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / "spec/execution-v2/contract.schema.json", target)


def sync(root: Path) -> None:
    source = root / "spec/1.0.0"
    for directory, names in (
        ("recorder/event/contract", {"event.schema.json", "event-types.json"}),
        (
            "analysis/src/sledra_analysis/resources/contract",
            {
                "event.schema.json",
                "event-types.json",
                "definition_manifest.schema.json",
                "run_context.schema.json",
                "attempt_context.schema.json",
                "job_ref.schema.json",
            },
        ),
        ("recorder/internal/speccontract/contract", {p.name for p in source.glob("*.json")}),
    ):
        target = root / directory
        target.mkdir(parents=True, exist_ok=True)
        for stale in target.glob("*.json"):
            if stale.name not in names:
                stale.unlink()
        for name in sorted(names):
            shutil.copyfile(source / name, target / name)
    sync_execution(root)


if __name__ == "__main__":
    sync(ROOT)
