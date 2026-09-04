"""Run the bounded Context Hub MVP acceptance suite on synthetic data only."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from context_hub import ContextHub
from context_hub.hub import SCHEMA_VERSION, sha256_text, utc_now


MCP_COLD_START_SAMPLES = 3
MCP_COLD_START_LIMIT_MS = 1_000.0


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _timing_summary(values: list[float]) -> dict[str, float]:
    return {
        "min_ms": round(min(values), 3),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


def _median_within_limit(values: list[float], limit_ms: float) -> bool:
    """Apply a stable threshold without hiding sustained startup regressions."""
    return statistics.median(values) <= limit_ms


def _event(index: int) -> dict[str, Any]:
    content = f"合成性能记录 {index:05d}，唯一检索标记为 合成检索编号{index:05d}。"
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"evt_{uuid.uuid4()}",
        "action": "append",
        "kind": "fact",
        "project_id": None,
        "content": content,
        "created_at": utc_now(),
        "source": {"type": "acceptance", "ref": f"synthetic:performance:{index}"},
        "supersedes": None,
        "tombstone": False,
        "content_sha256": sha256_text(content),
    }


def _write_synthetic_events(hub: ContextHub, count: int) -> None:
    with hub.events_path.open("w", encoding="utf-8", newline="\n") as handle:
        for index in range(count):
            handle.write(
                json.dumps(
                    _event(index),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
        handle.flush()
        os.fsync(handle.fileno())


async def _measure_mcp(
    data_dir: Path,
    *,
    query_indexes: Sequence[int],
    cold_start_samples: int = MCP_COLD_START_SAMPLES,
) -> tuple[list[float], dict[str, float]]:
    if cold_start_samples < 3 or cold_start_samples % 2 == 0:
        raise ValueError("cold_start_samples must be an odd integer of at least 3")

    environment = os.environ.copy()
    environment["CONTEXT_HUB_DATA_DIR"] = str(data_dir)
    environment.pop("CONTEXT_HUB_WRITE_ENABLED", None)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "context_hub.mcp_stdio"],
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
    )
    cold_timings: list[float] = []
    timings: list[float] = []
    for sample_index in range(cold_start_samples):
        launch_started = time.perf_counter()
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=10)
                await asyncio.wait_for(session.list_tools(), timeout=10)
                cold_timings.append((time.perf_counter() - launch_started) * 1000)

                # Reuse the final fresh process for the separate steady-state benchmark.
                if sample_index != cold_start_samples - 1:
                    continue
                await asyncio.wait_for(
                    session.call_tool(
                        "context_get",
                        {
                            "op": "search",
                            "query": f"合成检索编号{query_indexes[0]:05d}",
                            "limit": 3,
                            "max_chars": 600,
                        },
                    ),
                    timeout=10,
                )
                for index in query_indexes:
                    started = time.perf_counter()
                    result = await asyncio.wait_for(
                        session.call_tool(
                            "context_get",
                            {
                                "op": "search",
                                "query": f"合成检索编号{index:05d}",
                                "limit": 3,
                                "max_chars": 600,
                            },
                        ),
                        timeout=10,
                    )
                    if result.is_error or not result.structured_content:
                        raise RuntimeError(f"MCP search failed at synthetic record {index}")
                    if not result.structured_content.get("items"):
                        raise RuntimeError(f"MCP search missed synthetic record {index}")
                    timings.append((time.perf_counter() - started) * 1000)
    return cold_timings, _timing_summary(timings)


def run(*, records: int, queries: int) -> dict[str, Any]:
    if records < 10_000:
        raise ValueError("records must be at least 10000 for the v1.1 acceptance contract")
    if queries < 20:
        raise ValueError("queries must be at least 20")

    with tempfile.TemporaryDirectory(prefix="context-hub-acceptance-") as temporary:
        data_dir = Path(temporary)
        hub = ContextHub(data_dir)
        hub.initialize()
        _write_synthetic_events(hub, records)

        reindex_started = time.perf_counter()
        reindex_result = hub.reindex()
        reindex_ms = (time.perf_counter() - reindex_started) * 1000
        if reindex_result["events"] != records:
            raise RuntimeError("reindex count does not match the synthetic source")

        # Deterministic, evenly distributed record selection avoids random benchmark drift.
        indexes = [((position * 7919) % records) for position in range(queries)]
        fresh_hub = ContextHub(data_dir)
        cold_started = time.perf_counter()
        cold_result = fresh_hub.search(f"合成检索编号{indexes[0]:05d}")
        core_cold_ms = (time.perf_counter() - cold_started) * 1000
        if not cold_result["items"]:
            raise RuntimeError("core cold search missed its synthetic record")

        direct_timings: list[float] = []
        for index in indexes:
            started = time.perf_counter()
            result = hub.search(f"合成检索编号{index:05d}")
            if not result["items"]:
                raise RuntimeError(f"core search missed synthetic record {index}")
            direct_timings.append((time.perf_counter() - started) * 1000)

        mcp_count = min(max(20, queries // 5), 100)
        mcp_cold_samples, mcp_timings = asyncio.run(
            _measure_mcp(data_dir, query_indexes=indexes[:mcp_count])
        )
        mcp_cold = _timing_summary(mcp_cold_samples)
        doctor = hub.doctor()
        if not doctor["ok"] or doctor["counts"]["events"] != records:
            raise RuntimeError(f"doctor rejected the generated data root: {doctor['errors']}")

        direct = _timing_summary(direct_timings)
        checks = {
            "record_floor": records >= 10_000,
            "core_search_p50_le_50_ms": direct["p50_ms"] <= 50,
            "core_search_p95_le_150_ms": direct["p95_ms"] <= 150,
            "mcp_prewarm_p95_le_500_ms": mcp_timings["p95_ms"] <= 500,
            "mcp_cold_start_le_1000_ms": _median_within_limit(
                mcp_cold_samples, MCP_COLD_START_LIMIT_MS
            ),
            "doctor_exact_count": doctor["counts"]["events"] == records,
        }
        return {
            "ok": all(checks.values()),
            "synthetic_only": True,
            "records": records,
            "queries": {
                "core": queries,
                "mcp_cold_start": len(mcp_cold_samples),
                "mcp_prewarm": mcp_count,
            },
            "timings": {
                "reindex_ms": round(reindex_ms, 3),
                "core_cold_search_ms": round(core_cold_ms, 3),
                "core_search": direct,
                # Preserve the original scalar key for report consumers; it is now the median.
                "mcp_cold_start_and_list_ms": mcp_cold["p50_ms"],
                "mcp_cold_start_and_list": {
                    **mcp_cold,
                    "samples_ms": [round(value, 3) for value in mcp_cold_samples],
                },
                "mcp_prewarm_search": mcp_timings,
            },
            "checks": checks,
            "doctor_counts": doctor["counts"],
            "notes": [
                "All fact sources were generated inside one temporary directory.",
                "Core cold search is diagnostic; the v1.1 cold-start threshold applies to the STDIO MCP process.",
                "The MCP cold-start gate uses the median of three independent fresh processes; the 1000 ms target is unchanged.",
            ],
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=10_000)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run(records=args.records, queries=args.queries)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
