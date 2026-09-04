from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_hub import ContextHub


class MultiprocessWriteTest(unittest.TestCase):
    def test_four_processes_append_exactly_four_hundred_events(self) -> None:
        with tempfile.TemporaryDirectory(prefix="context-hub-concurrency-") as temporary:
            data_dir = Path(temporary) / "data"
            hub = ContextHub(data_dir)
            hub.initialize()
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            processes = [
                subprocess.Popen(
                    [sys.executable, "-m", "tests.concurrent_writer", str(data_dir), str(worker), "100"],
                    cwd=Path(__file__).resolve().parents[1],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                for worker in range(4)
            ]
            failures: list[str] = []
            for worker, process in enumerate(processes):
                stdout, stderr = process.communicate(timeout=120)
                if process.returncode != 0:
                    failures.append(f"worker {worker}: rc={process.returncode}\nstdout={stdout}\nstderr={stderr}")
            self.assertEqual(failures, [])

            lines = [line for line in hub.events_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(lines), 400)
            events = [json.loads(line) for line in lines]
            self.assertEqual(len({event["event_id"] for event in events}), 400)
            self.assertEqual(len({event["source"]["ref"] for event in events}), 400)
            diagnostic = hub.doctor()
            self.assertTrue(diagnostic["ok"], diagnostic)
            self.assertEqual(diagnostic["counts"]["events"], 400)
            self.assertEqual(diagnostic["counts"]["indexed_events"], 400)


if __name__ == "__main__":
    unittest.main()
