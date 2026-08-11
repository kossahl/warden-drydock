"""Fail-open Codex Stop hook for Agent Ascendry."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    try:
        payload = sys.stdin.buffer.read(65_537)
        completed = subprocess.run(
            [sys.executable, "-m", "agent_ascendry", "capture", str(root)],
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        )
        if completed.returncode != 0:
            print(
                "Agent Ascendry capture failed; run agent-ascendry validate .",
                file=sys.stderr,
            )
    except Exception:
        print(
            "Agent Ascendry capture failed; run agent-ascendry validate .",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
