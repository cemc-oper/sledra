"""Copy canonical generated resources into recorder and analysis resource directories."""

from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def sync(root: Path) -> None:
    source = root / "spec/1.0.0"
    for directory, names in (
        ("recorder/event/contract", {"event.schema.json", "event-types.json"}),
        (
            "analysis/src/sledra_analysis/resources/contract",
            {"event.schema.json", "event-types.json"},
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


if __name__ == "__main__":
    sync(ROOT)
