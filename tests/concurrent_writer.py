from __future__ import annotations

import argparse

from context_hub import ContextHub


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir")
    parser.add_argument("worker", type=int)
    parser.add_argument("count", type=int)
    args = parser.parse_args()

    hub = ContextHub(args.data_dir)
    for sequence in range(args.count):
        hub.put(
            action="append",
            kind="fact",
            content=f"synthetic concurrency worker={args.worker} sequence={sequence}",
            source_type="test",
            source_ref=f"worker:{args.worker}:{sequence}",
            confirmed=True,
        )


if __name__ == "__main__":
    main()
