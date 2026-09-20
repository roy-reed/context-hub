#!/usr/bin/env python3
"""Run the v0.2 Loop contract against isolated synthetic fixtures only."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any, Sequence

from context_hub import ContextHub
from context_hub.errors import ValidationError
from context_hub.loop import LoopCoordinator


def _write_project(root: Path, *, title: str, marker: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text(
        f"# {title}\n\n{marker} is isolated synthetic acceptance data.\n",
        encoding="utf-8",
        newline="\n",
    )


def run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="context-hub-loop-") as temporary:
        workspace = Path(temporary).resolve()
        data_root = workspace / "hub"
        alpha_root = workspace / "alpha"
        beta_root = workspace / "beta"
        import_root = workspace / "import-candidate"
        restored_root = workspace / "restored"

        alpha_marker = "alpha-loop-marker-72e4"
        beta_marker = "beta-loop-marker-91bf"
        _write_project(alpha_root, title="Synthetic Alpha", marker=alpha_marker)
        _write_project(beta_root, title="Synthetic Beta", marker=beta_marker)
        _write_project(import_root, title="Synthetic Import Candidate", marker="import-loop-marker-4c3a")

        hub = ContextHub(data_root)
        hub.initialize()
        hub.register_project("loop-alpha", alpha_root)
        hub.register_project("loop-beta", beta_root)
        loop = LoopCoordinator(hub)

        sync = loop.sync(force=True)
        status = loop.quick_status()
        backup_action = loop.loop_check(apply_safe=True)
        ready = loop.loop_check()

        cases_path = workspace / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "synthetic_only": True,
                    "cases": [
                        {
                            "id": "alpha",
                            "query": alpha_marker,
                            "expected_project_id": "loop-alpha",
                        },
                        {
                            "id": "beta",
                            "query": beta_marker,
                            "expected_project_id": "loop-beta",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
            newline="\n",
        )
        evaluation = loop.evaluate(cases_path)

        state_before_plan = loop.state_path.read_bytes()
        plan = loop.import_plan(
            project_id="future-real-project",
            root=import_root,
            classification="real",
        )
        state_after_plan = loop.state_path.read_bytes()
        approval = loop.approve_import(
            plan["plan_hash"],
            project_id="future-real-project",
            root=import_root,
            classification="real",
            confirmed=True,
        )
        (import_root / "AGENTS.md").write_text(
            "# Synthetic Import Candidate\n\nchanged-after-approval\n",
            encoding="utf-8",
            newline="\n",
        )
        changed_plan_rejected = False
        try:
            loop.approve_import(
                plan["plan_hash"],
                project_id="future-real-project",
                root=import_root,
                classification="real",
                confirmed=True,
            )
        except ValidationError:
            changed_plan_rejected = True

        exact_worker_policy = loop.worker_policy(max_total_tokens=1_000_000)
        over_worker_policy = loop.worker_policy(max_total_tokens=1_000_001)
        worker = loop.record_worker(
            task_id="synthetic-loop-worker-001",
            max_total_tokens=1_000_000,
            observed_tokens=1_006_464,
            attempts=3,
            output_chars=70_000,
            wall_seconds=60.9,
            outcome="token_budget",
        )
        wall_worker = loop.record_worker(
            task_id="synthetic-loop-worker-wall-001",
            max_total_tokens=1_000_000,
            observed_tokens=100,
            attempts=1,
            output_chars=100,
            wall_seconds=3_600.001,
            outcome="completed",
        )
        worker_review = loop.loop_check()
        fourth_attempt_rejected = False
        try:
            loop.record_worker(
                task_id="synthetic-loop-worker-002",
                max_total_tokens=1_000_000,
                observed_tokens=100,
                attempts=4,
                output_chars=100,
                wall_seconds=1.0,
                outcome="failed",
            )
        except ValidationError:
            fourth_attempt_rejected = True

        backup = hub.backup(workspace / "backups")
        restored = ContextHub.restore(backup["backup"], restored_root)
        restored_status = ContextHub(restored_root).doctor()
        backup_entries = {entry["path"] for entry in backup["entries"]}

        checks = {
            "forced_sync_current": sync["ok"] and sync["freshness"] == "current",
            "bounded_status_current": status["ok"] and status["freshness"] == "current",
            "one_safe_action_backup": backup_action["action"] == "backup" and backup_action["applied"],
            "loop_reaches_ready": ready["action"] == "ready" and ready["ready"],
            "two_project_eval_exact": (
                evaluation["ok"]
                and evaluation["top1_accuracy"] == 1.0
                and evaluation["top3_accuracy"] == 1.0
            ),
            "import_plan_dry_run_non_mutating": (
                plan["ok"]
                and plan["dry_run"]
                and not plan["mutated"]
                and state_before_plan == state_after_plan
            ),
            "import_approval_exact_snapshot": approval["approved"] and changed_plan_rejected,
            "million_token_boundary": exact_worker_policy["accepted"] and not over_worker_policy["accepted"],
            "worker_overshoot_recorded": (
                worker["worker"]["token_overshoot"]
                and worker["worker"]["output_truncated"]
                and worker["worker"]["protection_exceeded"]
                and worker["worker"]["attempts"] == 3
            ),
            "worker_wall_time_guarded": (
                wall_worker["worker"]["wall_time_exceeded"]
                and wall_worker["worker"]["protection_exceeded"]
                and wall_worker["worker"]["needs_review"]
                and worker_review["action"] == "inspect_worker"
            ),
            "fourth_attempt_rejected": fourth_attempt_rejected,
            "telemetry_default_off": not loop.telemetry_path.exists(),
            "authoritative_backup_scope": backup_entries
            == {"config.toml", "manifest.json", "memory/events.jsonl"},
            "restore_reindexes_and_passes_doctor": (
                restored["ok"] and restored["reindexed"] and restored_status["ok"]
            ),
        }
        return {
            "ok": all(checks.values()),
            "synthetic_only": True,
            "version_contract": "0.2.0",
            "projects": 2,
            "evaluation_cases": evaluation["cases"],
            "worker_limits": worker["policy"],
            "checks": checks,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run()
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
