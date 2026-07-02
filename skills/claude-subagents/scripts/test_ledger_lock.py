#!/usr/bin/env python3
"""Self-check: concurrent save_ledger calls must not race or drop entries.
Run: python3 test_ledger_lock.py
"""
import multiprocessing
import tempfile
from pathlib import Path

from run_claude_subagent import ledger_lock, load_ledger, save_ledger


def _writer(ledger_path_str: str, task_id: str) -> None:
    path = Path(ledger_path_str)
    with ledger_lock(path):
        ledger = load_ledger(path)
        ledger["runs"] = [r for r in ledger["runs"] if r["task_id"] != task_id]
        ledger["runs"].append({"task_id": task_id, "status": "complete"})
        save_ledger(path, ledger)


def demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        save_ledger(ledger_path, {"runs": []})
        task_ids = [f"task-{i}" for i in range(20)]

        procs = [
            multiprocessing.Process(target=_writer, args=(str(ledger_path), tid))
            for tid in task_ids
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
            assert p.exitcode == 0

        final = load_ledger(ledger_path)
        seen = {r["task_id"] for r in final["runs"]}
        assert seen == set(task_ids), f"lost entries: {set(task_ids) - seen}"
        assert not ledger_path.with_suffix(".tmp").exists(), "stale shared tmp file"

    print(f"ok: {len(task_ids)} concurrent writers, no lost ledger entries")


if __name__ == "__main__":
    demo()
