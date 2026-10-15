"""Shared process identity decisions without storage or clocks."""

import copy
import unittest

from sledra_analysis.process_pairing import process_pairing


class ProcessPairingTests(unittest.TestCase):
    def test_endpoints_and_identity_matrix(self):
        start = {
            "event_id": "start-a",
            "event_type": "attempt.started",
            "association": {"run_id": "run", "task_instance_id": "task", "attempt_id": "attempt"},
            "payload": {"process_role": "main", "invocation_id": "invocation-a"},
            "source": {"source_id": "node", "session_id": "session", "boot_id": "boot"},
            "occurred_at": "2026-10-10T00:00:00Z",
        }
        end = copy.deepcopy(start)
        end.update(event_id="end-a", event_type="attempt.exited")
        end["payload"] = {"process_role": "main", "termination": "exited", "exit_code": 0}
        end["occurred_at"] = "2026-10-10T00:00:01Z"

        def changed(event, field, key, value):
            result = copy.deepcopy(event)
            result[field][key] = value
            return result

        cases = {
            "paired": ([start], [end], ("paired", "process_identity_matched")),
            "same_report": (
                [start, dict(start, event_id="start-b")],
                [end],
                ("paired", "process_identity_matched"),
            ),
            "different_invocation": (
                [start, changed(start, "payload", "invocation_id", "invocation-b")],
                [end],
                ("conflict", "multiple_process_starts"),
            ),
            "different_end": (
                [start],
                [end, changed(end, "payload", "exit_code", 1)],
                ("conflict", "multiple_process_ends"),
            ),
            "run": (
                [start],
                [changed(end, "association", "run_id", "other")],
                ("conflict", "process_invocation_association_uncertain"),
            ),
            "task": (
                [start],
                [changed(end, "association", "task_instance_id", "other")],
                ("conflict", "process_invocation_association_uncertain"),
            ),
            "attempt": (
                [start],
                [changed(end, "association", "attempt_id", "other")],
                ("conflict", "process_invocation_association_uncertain"),
            ),
            "role": (
                [start],
                [changed(end, "payload", "process_role", "other")],
                ("conflict", "process_invocation_association_uncertain"),
            ),
            "source": (
                [start],
                [changed(end, "source", "source_id", "other")],
                ("conflict", "process_invocation_association_uncertain"),
            ),
            "session": (
                [start],
                [changed(end, "source", "session_id", "other")],
                ("conflict", "process_invocation_association_uncertain"),
            ),
            "boot": (
                [start],
                [changed(end, "source", "boot_id", "other")],
                ("conflict", "process_invocation_association_uncertain"),
            ),
            "unknown_source": (
                [start],
                [changed(end, "source", "session_id", None)],
                ("conflict", "process_identity_unavailable"),
            ),
            "start_only": ([start], [], ("start_only", "missing_process_end")),
            "end_only": ([], [end], ("end_only", "missing_process_start")),
        }
        for name, (starts, ends, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(process_pairing(starts, ends), expected)
