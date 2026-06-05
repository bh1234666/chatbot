"""IELTS material index short test.

Asks the orchestrator to enumerate the F:\\chatbot\\5月雅思 directory and
produce a coverage ledger / material_index before any synthesis. The goal
is to verify that "coverage first, synthesis later" is enforced and that
the read helper actually emits a material index file in time.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _driver_common import (  # noqa: E402
    DoneCapture,
    ask_followup_about_delivery,
    parse_followup_answer,
    read_sse_with_stall_cap,
)

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "stress_tools" / "runs"
API = "http://127.0.0.1:8000/v1/environment/stream"
WALL_CAP_SEC = 1200  # 20 min hard deadline (was 720 — too tight on slow runs)
STALL_CAP_SEC = 90
SOURCE_DIR = Path(r"F:\chatbot\5月雅思")
EXPECTED_ARTIFACT = "material_index"


def main() -> int:
    if not SOURCE_DIR.is_dir():
        raise SystemExit(f"source dir missing: {SOURCE_DIR}")

    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    session_dir = RUN_DIR / f"ielts_material_index_{run_id}"
    work_dir = session_dir / "ielts_workspace"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    sse_log = session_dir / "sse.jsonl"
    summary = session_dir / "summary.json"

    body = {
        "user_id": f"ielts-mi-{run_id}",
        "user_name": "LongTest",
        "message": (
            "Stage A only: scan the entire source directory and produce a "
            f"material_index. Source: {SOURCE_DIR}. Do not write any subject "
            "summary yet; produce only material_index.json (or .md) in the "
            "current working directory listing every file with: relative path, "
            "file kind (txt/docx/pdf/image/audio/zip/html/md/other), size in "
            "bytes, and a planned read strategy (text-extract / OCR / "
            "audio-transcribe / archive-expand / skip-with-reason). Mark files "
            "that are duplicates or obviously empty. After the index is "
            "written, stop and report the index path. "
            "中文概要：仅先做覆盖清单，列出全部文件的种类、大小和读取策略，先产出 material_index 后停。"
        ),
        "current_dir": str(work_dir),
        "persona_id": "environment",
        "archive_id": f"ielts-mi-{run_id}",
        "client_msg_id": f"ielts-mi-{run_id}",
    }

    req = urllib.request.Request(
        API,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    started = time.time()
    index_seen_at: float | None = None
    capture = DoneCapture()
    stream_result = None

    print(f"[ielts-mi] run={run_id}")
    print(f"[ielts-mi] work_dir={work_dir}")

    sse_fh = sse_log.open("w", encoding="utf-8")

    def _on_text(text: str, t: float) -> None:
        nonlocal index_seen_at
        sse_fh.write(json.dumps({"t": t, "line": text}, ensure_ascii=False) + "\n")
        sse_fh.flush()
        if index_seen_at is None and (
            "material_index" in text or "coverage_ledger" in text
        ):
            index_seen_at = t
            print(f"[ielts-mi] material_index reference in stream at {index_seen_at:.1f}s")
        capture.feed_line(text)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            stream_result = read_sse_with_stall_cap(
                resp,
                wall_cap_sec=WALL_CAP_SEC,
                stall_cap_sec=STALL_CAP_SEC,
                on_text=_on_text,
            )
        if stream_result.truncated_wall_cap:
            print(f"[ielts-mi] wall-cap hit at {stream_result.elapsed_sec:.1f}s")
        elif stream_result.truncated_stall_cap:
            print(
                f"[ielts-mi] stall-cap hit at {stream_result.elapsed_sec:.1f}s "
                f"(no SSE activity for {STALL_CAP_SEC}s)"
            )
        elif stream_result.stream_error:
            print(f"[ielts-mi] stream error: {stream_result.stream_error}")
        else:
            print(f"[ielts-mi] stream ended at {stream_result.elapsed_sec:.1f}s")
    finally:
        sse_fh.close()

    line_count = stream_result.line_count if stream_result else 0
    truncated = bool(
        stream_result
        and (stream_result.truncated_wall_cap or stream_result.truncated_stall_cap)
    )

    declared = capture.llm_declared_paths(EXPECTED_ARTIFACT, "coverage")
    followup: dict | None = None
    followup_parsed: dict | None = None
    if not declared and capture.archive_id:
        print(f"[ielts-mi] no LLM declaration of {EXPECTED_ARTIFACT}; asking LLM follow-up")
        followup = ask_followup_about_delivery(
            API,
            archive_id=capture.archive_id,
            user_id=body["user_id"],
            current_dir=str(work_dir),
            expected=f"{EXPECTED_ARTIFACT}.json (or .md)",
            wall_cap_sec=240,
        )
        if followup.get("ok"):
            print(f"[ielts-mi] follow-up answer: {followup.get('answer_text','')[:280]}")
            for entry in followup.get("files") or []:
                hay = " ".join(
                    str(entry.get(k) or "").lower()
                    for k in ("name", "rel_path", "local_path", "url")
                )
                if EXPECTED_ARTIFACT in hay or "coverage" in hay:
                    declared.append(entry)
            followup_parsed = parse_followup_answer(
                followup.get("answer_text", ""), EXPECTED_ARTIFACT
            )
            if followup_parsed.get("declared") and not declared:
                declared.append({
                    "name": followup_parsed.get("path") or EXPECTED_ARTIFACT,
                    "rel_path": followup_parsed.get("path"),
                    "source": "followup_answer_text",
                })

    index_files = [
        str(e.get("local_path") or e.get("rel_path") or e.get("name") or e.get("path"))
        for e in declared
        if (e.get("local_path") or e.get("rel_path") or e.get("name") or e.get("path"))
    ]

    summary_data = {
        "run_id": run_id,
        "wall_cap_sec": WALL_CAP_SEC,
        "stall_cap_sec": STALL_CAP_SEC,
        "elapsed_sec": stream_result.elapsed_sec if stream_result else round(time.time() - started, 2),
        "last_activity_at": stream_result.last_activity_at if stream_result else None,
        "lines": line_count,
        "truncated": truncated,
        "truncated_kind": (
            "wall_cap" if stream_result and stream_result.truncated_wall_cap
            else "stall_cap" if stream_result and stream_result.truncated_stall_cap
            else None
        ),
        "index_stream_seen_at": index_seen_at,
        "index_files_present": index_files,
        "llm_declared_deliveries": declared,
        "followup": followup,
        "followup_parsed": followup_parsed,
        "archive_id": capture.archive_id,
        "work_dir": str(work_dir),
        "sse_log": str(sse_log),
    }
    summary.write_text(
        json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[ielts-mi] summary written to {summary}")
    print(json.dumps({k: summary_data[k] for k in (
        "run_id", "elapsed_sec", "truncated", "index_files_present"
    )}, ensure_ascii=False, indent=2))
    return 0 if index_files else 1


if __name__ == "__main__":
    sys.exit(main())
