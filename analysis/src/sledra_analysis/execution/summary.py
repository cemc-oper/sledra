"""Per-file Execution reduction and explicit counts, without persistent query state."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import groupby

from .codec import ContractError, comparable, equivalent, timestamp
from .reader import Fact, FileReader, ReadIssue, RunLookup, RunReference, Source


@dataclass(frozen=True)
class Event:
    data: dict
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class Selection:
    status: str  # missing / unique / conflicting
    records: tuple[Event, ...]

    @property
    def attributes(self) -> dict | None:
        return self.records[0].data["attributes"] if self.status == "unique" else None


@dataclass(frozen=True)
class Duration:
    declared_ns: int | None
    wall_ns: int | None
    status: str


@dataclass(frozen=True)
class Stage:
    stage_id: str
    name: str | None
    start: Selection
    end: Selection
    duration: Duration
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class RunAssociation:
    status: str
    run_id: str | None
    reference: RunReference | None = None


@dataclass(frozen=True)
class ExecutionSummary:
    execution_id: str
    events: tuple[Event, ...]
    start: Selection
    context: Selection
    end: Selection
    duration: Duration
    stages: tuple[Stage, ...]
    conflicts: tuple[str, ...]
    read_issues: tuple[ReadIssue, ...]

    @property
    def started(self) -> bool:
        return bool(self.start.records)


def _select(events: tuple[Event, ...], name: str, collisions: set[str]) -> Selection:
    selected = tuple(event for event in events if event.data["name"] == name)
    if not selected:
        return Selection("missing", ())
    values = [comparable(event.data, "event")["attributes"] for event in selected]
    conflict = any(event.data["event_id"] in collisions for event in selected) or any(
        not equivalent(values[0], value) for value in values[1:]
    )
    return Selection("conflicting" if conflict else "unique", selected)


def _instant(selection: Selection) -> tuple[int, int] | None:
    values = {timestamp(event.data["occurred_at"]) for event in selection.records}
    return next(iter(values)) if len(values) == 1 else None


def _duration(start: Selection, end: Selection) -> Duration:
    if start.status != "unique" or end.status != "unique":
        return Duration(
            None, None, f"{start.status}_start" if start.status != "unique" else f"{end.status}_end"
        )
    declared = end.attributes.get("duration_ns")
    first, last = _instant(start), _instant(end)
    if first is None or last is None:
        return Duration(declared, None, "ambiguous_wall_time")
    wall = (last[0] - first[0]) * 1_000_000_000 + last[1] - first[1]
    return Duration(declared, wall, "negative_wall_time" if wall < 0 else "observed")


def summarize(records: Iterable[Fact | ReadIssue]) -> ExecutionSummary | None:
    """Reduce one Execution's validated facts, retaining every variant/location.

    Caller-supplied facts must come from the v2 reader/codec. No legal facts means
    no visible Execution. This function does not validate or repair source files.
    """
    events: list[Event] = []
    values: list[dict] = []
    indexes: dict[str, list[int]] = {}
    issues = []
    identity = None
    for item in records:
        if isinstance(item, ReadIssue):
            issues.append(item)
            continue
        record = item.data
        if item.kind != "event":
            raise ContractError("unknown_kind")
        if identity is not None and identity != record["execution_id"]:
            raise ContractError("execution_mismatch")
        identity = record["execution_id"]
        value = comparable(record, "event")
        candidates = indexes.setdefault(record["event_id"], [])
        for index in candidates:
            if equivalent(value, values[index]):
                previous = events[index]
                events[index] = Event(previous.data, previous.sources + (item.source,))
                break
        else:
            candidates.append(len(events))
            events.append(Event(record, (item.source,)))
            values.append(value)
    if identity is None:
        return None
    collisions = {key for key, candidates in indexes.items() if len(candidates) > 1}
    all_events = tuple(events)
    start = _select(all_events, "execution.started", collisions)
    context = _select(all_events, "execution.context", collisions)
    end = _select(all_events, "execution.ended", collisions)
    conflicts = {"conflicting_event_id"} if collisions else set()
    for label, selection in (("start", start), ("context", context), ("end", end)):
        if selection.status == "conflicting":
            conflicts.add(f"conflicting_{label}")
    stages: dict[str, list[Event]] = {}
    for event in all_events:
        if event.data["name"] in ("stage.started", "stage.ended"):
            stages.setdefault(event.data["attributes"]["stage_id"], []).append(event)
    intervals = []
    for stage_id, facts in stages.items():
        first = _select(tuple(facts), "stage.started", collisions)
        last = _select(tuple(facts), "stage.ended", collisions)
        names = {event.data["attributes"]["stage"] for event in facts}
        errors = set()
        if first.status == "conflicting":
            errors.add("conflicting_stage_start")
        if last.status == "conflicting":
            errors.add("conflicting_stage_end")
        if len(names) != 1:
            errors.add("stage_name_mismatch")
        duration = (
            _duration(first, last) if not errors else Duration(None, None, "conflicting_stage")
        )
        intervals.append(
            Stage(
                stage_id,
                next(iter(names)) if len(names) == 1 else None,
                first,
                last,
                duration,
                tuple(sorted(errors)),
            )
        )
        conflicts.update(errors)
    return ExecutionSummary(
        identity,
        all_events,
        start,
        context,
        end,
        _duration(start, end),
        tuple(intervals),
        tuple(sorted(conflicts)),
        tuple(issues),
    )


def iter_summaries(
    reader: FileReader, record_from, record_through
) -> Iterator[ExecutionSummary | ReadIssue]:
    """Reduce one physical file at a time; do not load all history into memory.

    A valid layout has one file per Execution. group_counts additionally detects
    duplicate IDs across files and excludes their uncertain business grouping.
    """
    rows = reader.iter_events(record_from, record_through)
    try:
        for _, items in groupby(rows, key=lambda item: item.source.path):
            # Retain this file's issues even if it contains no legal facts.
            issues = []

            def facts():
                for item in items:
                    if isinstance(item, ReadIssue):
                        issues.append(item)
                    yield item

            summary = summarize(facts())
            if summary is None:
                yield from issues
            else:
                yield summary
    finally:
        rows.close()


def associate_run(summary: ExecutionSummary, lookup: RunLookup | None) -> RunAssociation:
    """Explain an already recorded association; never accept a runtime candidate."""
    context = summary.context.attributes
    if context is None:
        return RunAssociation("context_" + summary.context.status, None)
    identity = context["run_id"]
    if identity is None:
        return RunAssociation("unlinked", None)
    reference = lookup.references.get(identity) if lookup is not None else None
    if reference is None:
        return RunAssociation("not_loaded", identity)
    if reference.status != "found":
        return RunAssociation(reference.status, identity, reference)
    run = reference.records[0].data
    if run["scope"] != context["scope"]:
        return RunAssociation("scope_mismatch", identity, reference)
    kind = run["cycle_kind"]
    compatible = (
        kind == "multiple"
        or kind == context["cycle_kind"] == "none"
        or kind == "single"
        and context["cycle_kind"] == "none"
        or kind == context["cycle_kind"] == "single"
        and timestamp(run["cycle"]) == timestamp(context["cycle"])
    )
    return RunAssociation("associated" if compatible else "cycle_mismatch", identity, reference)


def events_for_target_cycle(summary: ExecutionSummary, cycle: str) -> tuple[Event, ...]:
    target = timestamp(cycle)
    selected = []
    for event in summary.events:
        if event.data["name"].startswith(("execution.", "stage.")):
            continue
        value = event.data["attributes"].get("target_cycle")
        if not isinstance(value, str):
            continue
        try:
            matches = timestamp(value) == target
        except ContractError:
            continue
        if matches:
            selected.append(event)
    return tuple(selected)


@dataclass(frozen=True)
class Counts:
    visible: int
    started: int
    missing_start: int


@dataclass(frozen=True)
class CountGroup:
    key: dict
    counts: Counts


@dataclass(frozen=True)
class Grouping:
    total: Counts
    groups: tuple[CountGroup, ...]
    uncertain: Counts
    duplicate_execution_ids: tuple[str, ...]
    read_issues: tuple[ReadIssue, ...]


def _scalar(value):
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, str):
        return "text", value
    if value is None:
        return "null", None
    return "number", value  # int/Decimal share exact equality and hashes.


def _group_key(summary, by, lookup):
    context = summary.context.attributes
    if context is None:
        return None
    key = {"scope": context["scope"]}
    if "task" in by:
        key["task"] = context["task"]
    if "parameters" in by:
        key["parameters"] = tuple(sorted((k, _scalar(v)) for k, v in context["parameters"].items()))
    if "cycle" in by:
        key["cycle_kind"] = context["cycle_kind"]
        key["cycle"] = timestamp(context["cycle"]) if context["cycle"] is not None else None
    if "run" in by:
        association = associate_run(summary, lookup)
        if association.status not in ("associated", "unlinked"):
            return None
        key["run_id"] = association.run_id
    display = {name: context[name] for name in key}
    return tuple(sorted(key.items())), display


def group_counts(
    summaries: Iterable[ExecutionSummary | ReadIssue],
    *,
    namespace: dict,
    by: tuple[str, ...] = ("task", "parameters", "cycle", "run"),
    runs: RunLookup | None = None,
) -> Grouping:
    """Count one root's observed IDs. Keep only compact membership per Execution.

    Every key includes namespace and scope. Default grouping preserves all four
    dimensions. Explicit rollups may select a subset. Unknown memberships remain
    separate from known null/none/multiple. No inferred Run, retry or success.
    """
    if not by or len(set(by)) != len(by) or set(by) - {"task", "parameters", "cycle", "run"}:
        raise ContractError("invalid_grouping")
    members = {}  # ID -> (has any start, key or None); never retain business events.
    displays = {}
    duplicates = set()
    issues = []
    for item in summaries:
        if isinstance(item, ReadIssue):
            issues.append(item)
            continue
        issues.extend(item.read_issues)
        identity = item.execution_id
        if identity in members:
            duplicates.add(identity)
            members[identity] = members[identity][0] or item.started, None
            continue
        group = _group_key(item, by, runs)
        key, display = group if group is not None else (None, None)
        members[identity] = item.started, key
        if key is not None:
            displays[key] = {"namespace": dict(namespace), **display}
    buckets = {}
    for started, key in members.values():
        counts = buckets.setdefault(key, [0, 0, 0])
        counts[0] += 1
        counts[1] += started
        counts[2] += not started
    total = Counts(
        len(members),
        sum(started for started, _ in members.values()),
        sum(not started for started, _ in members.values()),
    )
    return Grouping(
        total,
        tuple(
            CountGroup(displays[key], Counts(*counts))
            for key, counts in buckets.items()
            if key is not None
        ),
        Counts(*buckets.get(None, (0, 0, 0))),
        tuple(sorted(duplicates)),
        tuple(issues),
    )
