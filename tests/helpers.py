from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_markdown_project(root: Path, *, title: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "AGENTS.md"
    source.write_text(f"# {title}\n\n{body}\n", encoding="utf-8", newline="\n")
    return source
