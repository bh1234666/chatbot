"""DB paper stage 1: framework-only short test.

Streams ``/v1/environment/stream`` with a focused instruction asking the
orchestrator to build only the framework contract for the database-index
algorithm paper. A wall-clock cap prevents long runs while we evaluate
whether the framework helper produces ``framework_contract.md`` quickly
enough.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "stress_tools" / "runs"
API = "http://127.0.0.1:8000/v1/environment/stream"
WALL_CAP_SEC = 600  # hard cap: 10 minutes


def main() -> int:
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    session_dir = RUN_DIR / f"db_paper_stage1_framework_{run_id}"
    work_dir = session_dir / "db_index_paper_framework"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    sse_log = session_dir / "sse.jsonl"
    summary = session_dir / "summary.json"

    body = {
        "user_id": f"db-stage1-{run_id}",
        "user_name": "LongTest",
        "message": (
            "Stage 1: build only the framework contract for a paper that compares "
            "red-black tree, skip list, B-tree and B+ tree as database index "
            "structures and proposes one new structure. Produce framework_contract.md "
            "in the working directory with: paper outline, slice list (one per "
            "structure plus the new one), per-slice acceptance checks, and final "
            "DOCX assembly contract. Do not write any algorithm slice content yet. "
            "中文概要：本阶段仅产出框架契约 framework_contract.md，不写算法切片。"
        ),
        "current_dir": str(work_dir),
        "persona_id": "environment",
        "archive_id": f"db-stage1-{run_id}",
        "client_msg_id": f"db-stage1-{run_id}",
    }

    req = urllib.request.Request(
        API,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    started = time.time()
    framework_seen_at: float | None = None
    last_event_at = started
    line_count = 0
    truncated = False

    print(f"[stage1] run={run_id}")
    print(f"[stage1] work_dir={work_dir}")
    print(f"[stage1] sse_log={sse_log}")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp, sse_log.open(
            "w", encoding="utf-8"
        ) as fh:
            while True:
                elapsed = time.time() - started
                if elapsed > WALL_CAP_SEC:
                    truncated = True
                    print(f"[stage1] wall-cap hit at {elapsed:.1f}s; closing")
                    break
                line = resp.readline()
                if not line:
                    print(f"[stage1] stream ended at {elapsed:.1f}s")
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if not text:
                    continue
                last_event_at = time.time()
                fh.write(
                    json.dumps(
                        {"t": round(time.time() - started, 2), "line": text},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                fh.flush()
                line_count += 1
                if framework_seen_at is None and (
                    "framework_contract.md" in text or "framework_contract" in text
                ):
                    framework_seen_at = time.time() - started
                    print(
                        f"[stage1] framework_contract reference in stream at {framework_seen_at:.1f}s"
                    )
    except Exception as exc:  # noqa: BLE001
        print(f"[stage1] stream error: {exc!r}")

    # Inspect produced artifacts
    framework_paths = list(work_dir.rglob("framework_contract*"))
    contract_present = False
    contract_size = 0
    contract_path: str | None = None
    for p in framework_paths:
        if p.is_file():
            contract_present = True
            contract_size = p.stat().st_size
            contract_path = str(p.relative_to(work_dir))
            break

    summary_data = {
        "run_id": run_id,
        "stage": 1,
        "wall_cap_sec": WALL_CAP_SEC,
        "elapsed_sec": round(time.time() - started, 2),
        "lines": line_count,
        "truncated": truncated,
        "framework_stream_seen_at": framework_seen_at,
        "framework_contract_present": contract_present,
        "framework_contract_size": contract_size,
        "framework_contract_path": contract_path,
        "framework_paths_found": [str(p.relative_to(work_dir)) for p in framework_paths],
        "work_dir": str(work_dir),
        "sse_log": str(sse_log),
    }
    summary.write_text(
        json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[stage1] summary written to {summary}")
    print(json.dumps(summary_data, ensure_ascii=False, indent=2))
    return 0 if contract_present else 1


if __name__ == "__main__":
    sys.exit(main())
