"""Query Execution v2 files directly; no import, database, or persistent query state."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import asdict, replace
from decimal import Decimal

from .codec import UUID, ContractError, decode, equivalent, timestamp
from .reader import FileReader, ReadIssue
from .summary import Selection, associate_run, events_for_target_cycle, group_counts, iter_summaries


def _date(value: str) -> dt.date:
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise ValueError
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a valid YYYY-MM-DD date") from None


def _time(value: str) -> str:
    try:
        timestamp(value)
    except ContractError:
        raise argparse.ArgumentTypeError("expected RFC3339 time with a known offset") from None
    return value


def _uuid(value: str) -> str:
    if UUID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("expected a lowercase UUIDv4")
    return value


def _parameter(value: str):
    key, separator, raw = value.partition("=")
    if not separator or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None:
        raise argparse.ArgumentTypeError("expected snake_case_key=JSON_scalar")
    try:
        item = decode(raw.encode("utf-8"))
    except (ContractError, UnicodeError):
        raise argparse.ArgumentTypeError("invalid bounded JSON scalar") from None
    if isinstance(item, (dict, list)):
        raise argparse.ArgumentTypeError("parameter filters require JSON scalars")
    return key, item


def _event_name(value: str) -> str:
    if (
        len(value) > 128
        or re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", value) is None
        or value.startswith(("execution.", "stage."))
    ):
        raise argparse.ArgumentTypeError("expected a business event name")
    return value


def _parser():
    parser = argparse.ArgumentParser(
        prog="sledra-history",
        description="Read Execution v2 history directly from files.",
        epilog="Record dates select entry partitions; cycle/ID filters never widen them. "
        "Finite windows are not all history. Missing end does not mean running.",
        allow_abbrev=False,
    )
    parser.add_argument("--root", required=True, help="existing absolute v2 record root")
    parser.add_argument("--record-from", type=_date, help="inclusive record date YYYY-MM-DD")
    parser.add_argument("--record-through", type=_date, help="inclusive record date YYYY-MM-DD")
    parser.add_argument(
        "--all-record-dates", action="store_true", help="explicitly scan all retained record dates"
    )
    parser.add_argument("--task", help="exact final-context task")
    parser.add_argument("--scope", help="exact final-context scope")
    parser.add_argument(
        "--parameter",
        type=_parameter,
        action="append",
        default=[],
        metavar="KEY=JSON",
        help="repeatable exact scalar predicate; all supplied keys must match",
    )
    parser.add_argument(
        "--cycle", type=_time, help="Execution's final single cycle, not a record date"
    )
    parser.add_argument("--cycle-kind", choices=("single", "none", "multiple"))
    parser.add_argument(
        "--run-id", type=_uuid, help="recorded final Run reference; inspect association status"
    )
    parser.add_argument(
        "--execution-id", type=_uuid, help="Execution ID within the selected record dates"
    )
    parser.add_argument("--event", type=_event_name, help="require this business event")
    parser.add_argument(
        "--target-cycle", type=_time, help="require a business event targeting this cycle"
    )
    parser.add_argument(
        "--format",
        choices=("text", "jsonl"),
        default="text",
        help="readable text or streaming exact JSONL (default: text)",
    )
    parser.add_argument(
        "--group-by",
        default="task,parameters,cycle,run",
        metavar="DIMENSIONS",
        help="comma-separated subset of task,parameters,cycle,run; namespace/scope always retained",
    )
    return parser


def _arguments(argv):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.all_record_dates and (args.record_from is not None or args.record_through is not None):
        parser.error("--all-record-dates conflicts with explicit record dates")
    if (args.record_from is None) != (args.record_through is None):
        parser.error("provide both --record-from and --record-through")
    if args.record_from is not None and args.record_from > args.record_through:
        parser.error("record date window is reversed")
    if args.cycle is not None and args.cycle_kind not in (None, "single"):
        parser.error("--cycle requires --cycle-kind single when a kind is supplied")
    if len({key for key, _ in args.parameter}) != len(args.parameter):
        parser.error("duplicate parameter filter keys")
    args.group_by = tuple(args.group_by.split(","))
    if (
        not args.group_by
        or len(set(args.group_by)) != len(args.group_by)
        or set(args.group_by) - {"task", "parameters", "cycle", "run"}
    ):
        parser.error("invalid --group-by dimensions")
    return args


def _window(args, config, now):
    if args.all_record_dates:
        return None, None, "all_retained"
    if args.record_from is not None:
        return args.record_from, args.record_through, "explicit"
    zone = config["partition_timezone"]
    minutes = (int(zone[1:3]) * 60 + int(zone[4:6])) * (1 if zone[0] == "+" else -1)
    today = now.astimezone(dt.timezone(dt.timedelta(minutes=minutes))).date()
    first = dt.date.fromordinal(max(1, today.toordinal() - 6))
    return first, today, "default_seven_days"


def _json(value) -> str:
    """Emit exact JSON numbers; no Decimal -> binary64 or string conversion."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _json(v) for k, v in value.items())
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json(v) for v in value) + "]"
    # POSIX error locations may contain surrogate-escaped filename bytes. Keep
    # the output valid UTF-8 even for those rejected layout entries.
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        .encode("utf-8", "backslashreplace")
        .decode("utf-8")
    )


def _display_time(value, zone):
    if value is None:
        return None
    seconds, nanos = timestamp(value)
    minutes = (int(zone[1:3]) * 60 + int(zone[4:6])) * (1 if zone[0] == "+" else -1)
    seconds += minutes * 60
    try:
        day = dt.date.fromordinal(seconds // 86400)
    except ValueError:
        return None  # Original RFC3339 fact remains available at the year boundary.
    hour, rest = divmod(seconds % 86400, 3600)
    minute, second = divmod(rest, 60)
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:{second:02d}.{nanos:09d}{zone}"


def _emit(row, mode):
    if mode == "jsonl":
        print(_json(row))
    elif row["type"] == "coverage":
        print("Coverage: " + _json({k: v for k, v in row.items() if k != "type"}))
        print(
            "Scope: retained observations only; a finite window is not all history. "
            "No workflow/product completeness or live-running inference."
        )
    elif row["type"] == "execution":
        end = {"missing": "not_observed", "unique": "observed", "conflicting": "conflicting"}[
            row["end_status"]
        ]
        print(
            f"Execution {row['execution_id']}: started={row['started']} context={row['context_status']} end={end}"
        )
        for key in (
            "entry",
            "context",
            "cycle_display",
            "result",
            "association",
            "duration",
            "conflicts",
        ):
            print(f"  {key}: " + _json(row[key]))
        for key in ("stages", "events"):
            for value in row[key]:
                print(f"  {key[:-1]}: " + _json(value))
        if row["matched_event_ids"] is not None:
            print("  matched_event_ids: " + _json(row["matched_event_ids"]))
    else:
        print(
            row["type"].capitalize() + ": " + _json({k: v for k, v in row.items() if k != "type"})
        )


def _business_events(summary, args):
    if args.target_cycle is not None:
        events = events_for_target_cycle(summary, args.target_cycle)
    else:
        events = tuple(
            e for e in summary.events if not e.data["name"].startswith(("execution.", "stage."))
        )
    return tuple(e for e in events if args.event is None or e.data["name"] == args.event)


def _matches(summary, args):
    if args.execution_id is not None and summary.execution_id != args.execution_id:
        return False
    context_filter = any(
        value is not None
        for value in (args.task, args.scope, args.cycle, args.cycle_kind, args.run_id)
    ) or bool(args.parameter)
    if context_filter:
        attrs = summary.context.attributes
        if attrs is None:
            return False
        for flag, key in (
            (args.task, "task"),
            (args.scope, "scope"),
            (args.cycle_kind, "cycle_kind"),
            (args.run_id, "run_id"),
        ):
            if flag is not None and attrs[key] != flag:
                return False
        if args.cycle is not None and (
            attrs["cycle_kind"] != "single" or timestamp(attrs["cycle"]) != timestamp(args.cycle)
        ):
            return False
        if any(
            key not in attrs["parameters"] or not equivalent(attrs["parameters"][key], value)
            for key, value in args.parameter
        ):
            return False
    return not (args.event is not None or args.target_cycle is not None) or bool(
        _business_events(summary, args)
    )


def _execution(summary, args, lookup, zone):
    association = associate_run(summary, lookup)
    return {
        "type": "execution",
        "execution_id": summary.execution_id,
        "started": summary.started,
        "start_status": summary.start.status,
        "entry": summary.start.attributes,
        "context_status": summary.context.status,
        "context": summary.context.attributes,
        "cycle_display": _display_time(summary.context.attributes["cycle"], zone)
        if summary.context.attributes is not None
        else None,
        "end_status": summary.end.status,
        "result": summary.end.attributes,
        "duration": asdict(summary.duration),
        "conflicts": summary.conflicts,
        "association": {"status": association.status, "run_id": association.run_id},
        "stages": [
            {
                "stage_id": s.stage_id,
                "name": s.name,
                "start_status": s.start.status,
                "end_status": s.end.status,
                "result": s.end.attributes,
                "duration": asdict(s.duration),
                "conflicts": s.conflicts,
            }
            for s in summary.stages
        ],
        "events": [asdict(e) for e in summary.events],
        "matched_event_ids": [e.data["event_id"] for e in _business_events(summary, args)]
        if args.event is not None or args.target_cycle is not None
        else None,
    }


def _query(reader, args, now):
    config = reader.config
    first, last, mode = _window(args, config, now)
    window = (first, last)
    coverage = {
        "type": "coverage",
        "format_version": config["format_version"],
        "layout_version": config["layout_version"],
        "mode": mode,
        "record_from": first.isoformat() if first else None,
        "record_through": last.isoformat() if last else None,
        "partition_timezone": config["partition_timezone"],
        "namespace": config["namespace"],
        "finite_window": first is not None,
        "snapshot": False,
        "run_metadata_scope": "references in scanned window plus requested run_id; all retained Run creation dates",
        "filters": {
            key: getattr(args, key)
            for key in (
                "task",
                "scope",
                "parameter",
                "cycle",
                "cycle_kind",
                "run_id",
                "execution_id",
                "event",
                "target_cycle",
            )
        },
        "group_by": args.group_by,
    }
    _emit(coverage, args.format)
    paths, starts, duplicates, run_ids = {}, set(), set(), set()
    observed_issues = set()
    reasons, errors = set(), set()
    unresolved_context = set()

    def issue(item, phase):
        if (phase, item) not in observed_issues:
            observed_issues.add((phase, item))
            _emit({"type": "issue", "scope": phase, **asdict(item)}, args.format)
        errors.add("read_errors")
        reasons.add("read_errors")

    # Survey compact membership/references first; do not retain event histories.
    # The second pass streams details and counts. Neither pass is an atomic snapshot.
    for item in iter_summaries(reader, *window):
        if isinstance(item, ReadIssue):
            issue(item, "executions")
            continue
        for problem in item.read_issues:
            issue(problem, "executions")
        identity = item.execution_id
        path = item.events[0].sources[0].path
        if identity in paths and paths[identity] != path:
            duplicates.add(identity)
        paths[identity] = path
        if item.started:
            starts.add(identity)
        if item.context.status != "unique":
            unresolved_context.add(identity)
        if item.conflicts:
            errors.add("conflicting_facts")
            reasons.add("conflicting_facts")
        for event in item.context.records:
            reference = event.data["attributes"]["run_id"]
            if reference is not None:
                run_ids.add(reference)
    if args.run_id is not None:
        run_ids.add(args.run_id)
    lookup = reader.resolve_runs(run_ids)
    for problem in lookup.issues:
        issue(problem, "runs")
    for identity, reference in sorted(lookup.references.items()):
        _emit(
            {
                "type": "run",
                "run_id": identity,
                "status": reference.status,
                "records": [asdict(r) for r in reference.records],
            },
            args.format,
        )
        if reference.status != "found":
            errors.add("uncertain_run_reference")
            reasons.add("uncertain_run_reference")
    seen = set()
    selected_count = 0

    def selected():
        nonlocal selected_count
        for item in iter_summaries(reader, *window):
            if isinstance(item, ReadIssue):
                issue(item, "executions")
                continue
            for problem in item.read_issues:
                issue(problem, "executions")
            identity = item.execution_id
            seen.add(identity)
            if (
                identity not in paths
                or paths[identity] != item.events[0].sources[0].path
                and identity not in duplicates
            ):
                reasons.add("scan_membership_changed")
                errors.add("scan_membership_changed")
            if identity in duplicates:
                item = replace(
                    item,
                    context=Selection("conflicting", item.context.records),
                    conflicts=tuple(sorted(set(item.conflicts) | {"multiple_execution_files"})),
                )
                reasons.add("multiple_execution_files")
                errors.add("multiple_execution_files")
            if item.conflicts:
                errors.add("conflicting_facts")
                reasons.add("conflicting_facts")
            if not _matches(item, args):
                continue
            selected_count += 1
            if not item.started:
                reasons.add("missing_start")
            if item.context.status != "unique":
                reasons.add("context_" + item.context.status)
            if item.end.status != "unique":
                reasons.add("end_" + item.end.status)
            if any(s.start.status != "unique" or s.end.status != "unique" for s in item.stages):
                reasons.add("incomplete_stage")
            for duration in [item.duration, *(s.duration for s in item.stages)]:
                if duration.status in ("negative_wall_time", "ambiguous_wall_time"):
                    reasons.add(duration.status)
            association = associate_run(item, lookup)
            if association.status not in (
                "associated",
                "unlinked",
                "context_missing",
                "context_conflicting",
            ):
                errors.add("uncertain_run_reference")
                reasons.add("uncertain_run_reference")
            _emit(_execution(item, args, lookup, config["partition_timezone"]), args.format)
            yield item

    counts = group_counts(selected(), namespace=config["namespace"], by=args.group_by, runs=lookup)
    if seen != set(paths):
        errors.add("scan_membership_changed")
        reasons.add("scan_membership_changed")
    for group in counts.groups:
        row = {"type": "group", **asdict(group)}
        if "cycle" in row["key"]:
            row["key"]["cycle_display"] = _display_time(
                row["key"]["cycle"], config["partition_timezone"]
            )
        _emit(row, args.format)
    _emit(
        {
            "type": "summary",
            "scanned": {
                "visible": len(paths),
                "started": len(starts),
                "missing_start": len(paths) - len(starts),
            },
            "matched": asdict(counts.total),
            "uncertain": asdict(counts.uncertain),
            "window_unresolved_context_ids": sorted(unresolved_context),
            "duplicate_execution_ids": sorted(duplicates | set(counts.duplicate_execution_ids)),
            "execution_rows": selected_count,
            "issue_count": len(observed_issues),
            "incomplete": bool(reasons),
            "incomplete_reasons": sorted(reasons),
            "no_matches": counts.total.visible == 0,
            "coverage": {
                k: coverage[k] for k in ("mode", "record_from", "record_through", "finite_window")
            },
            "notice": "Counts cover observed facts in this window, not all real starts or workflow/product completion; no matches does not prove absence outside it.",
        },
        args.format,
    )
    return 1 if errors else 0


def main(argv=None) -> int:
    args = _arguments(argv)
    try:
        with FileReader(args.root) as reader:
            return _query(reader, args, dt.datetime.now(dt.timezone.utc))
    except ContractError as exc:
        print(_json({"type": "error", "code": exc.code}), file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except OSError:
        print(_json({"type": "error", "code": "query_io_error"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
