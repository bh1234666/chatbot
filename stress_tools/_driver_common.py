"""Shared driver helpers — let the LLM be the source of truth for delivery.

Three pieces:

1. ``DoneCapture`` parses SSE lines and remembers every ``event: done`` payload.
   The orchestrator publishes ``files: [{name, local_path, rel_path, url}]``
   there — that's the LLM's own delivery report, no filesystem guessing needed.

2. ``read_sse_with_stall_cap`` reads an SSE response with two limits: a
   hard wall-cap and a "no-activity" stall-cap. The orchestrator emits
   `: ping` lines every ~15s, so stalls are real (server crashed, helper
   wedged, network dead). A pure wall-cap cuts off in-flight final-assembly
   helpers, leading to the LLM truthfully but uselessly saying
   "not delivered" when asked 3 seconds before the artifact lands.

3. ``ask_followup_about_delivery`` posts a second turn on the same archive
   when the first stream truly ends without naming the expected artifact.
   The LLM's answer is the source of truth; if it says "not delivered",
   the driver records that verbatim and exits non-zero — no filesystem
   fallback, no auto-discovery.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, IO


@dataclass
class StreamReadResult:
    elapsed_sec: float
    line_count: int
    truncated_wall_cap: bool
    truncated_stall_cap: bool
    last_activity_at: float
    stream_error: str | None


def read_sse_with_stall_cap(
    response,
    *,
    wall_cap_sec: float,
    stall_cap_sec: float,
    on_text: Callable[[str, float], None],
) -> StreamReadResult:
    """Read SSE bytes from `response` until the stream ends, the hard
    deadline `wall_cap_sec` elapses, OR no bytes arrive for `stall_cap_sec`.

    `on_text` is called with each decoded non-empty line (raw, not split).
    Pings (`: ping ...`) count as activity — they reset the stall timer.
    """
    started = time.time()
    last_activity = started
    line_count = 0
    truncated_wall = False
    truncated_stall = False
    err: str | None = None

    # urllib's HTTPResponse.readline blocks; we need timeout enforcement at
    # the socket level. Drop the connection's read timeout to ~stall_cap so
    # readline() returns within the stall window even if no bytes flow.
    try:
        sock = response.fp.raw._sock  # noqa: SLF001
    except AttributeError:
        sock = None
    try:
        if sock is not None:
            sock.settimeout(min(stall_cap_sec, 30.0))
    except Exception:
        pass

    try:
        while True:
            now = time.time()
            if now - started > wall_cap_sec:
                truncated_wall = True
                break
            if now - last_activity > stall_cap_sec:
                truncated_stall = True
                break
            try:
                line = response.readline()
            except Exception as exc:  # socket timeout, connection reset, etc.
                if "timed out" in str(exc).lower():
                    # Socket timed out waiting; loop back and check stall budget.
                    continue
                err = f"{type(exc).__name__}: {exc}"
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            if not text:
                continue
            last_activity = time.time()
            line_count += 1
            on_text(text, round(last_activity - started, 2))
    finally:
        try:
            if sock is not None:
                sock.settimeout(None)
        except Exception:
            pass

    return StreamReadResult(
        elapsed_sec=round(time.time() - started, 2),
        line_count=line_count,
        truncated_wall_cap=truncated_wall,
        truncated_stall_cap=truncated_stall,
        last_activity_at=round(last_activity - started, 2),
        stream_error=err,
    )


@dataclass
class DoneCapture:
    """Tracks the orchestrator's `event: done` payloads from an SSE stream."""

    archive_id: str | None = None
    done_files: list[dict[str, Any]] = field(default_factory=list)
    done_payloads: list[dict[str, Any]] = field(default_factory=list)
    artifact_announcements: list[dict[str, Any]] = field(default_factory=list)
    final_assistant_text: list[str] = field(default_factory=list)
    saw_complete: bool = False

    def feed_line(self, text: str) -> None:
        if not text or not text.startswith("data: "):
            return
        try:
            ev = json.loads(text[6:].rstrip("\r\n"))
        except Exception:
            return
        if not isinstance(ev, dict):
            return
        if self.archive_id is None:
            aid = str(ev.get("archive_id") or "").strip()
            if aid:
                self.archive_id = aid
        # The done event published right before stream close has the canonical
        # delivery list. Different orchestrator paths emit it with different
        # surrounding keys (`tendencies`, `files`, `trace_id`).
        if "files" in ev and isinstance(ev["files"], list):
            for entry in ev["files"]:
                if isinstance(entry, dict):
                    self.done_payloads.append(ev)
                    self.done_files.append(entry)
                    break
        # `artifact_ready` workflow events are the LLM's structured mid-stream
        # delivery announcement — emitted when the orchestrator commits a
        # final artifact to the main archive workspace. The shape is:
        #   {"kind":"artifact_ready", "path": "...", "artifact_type":"document"}
        if ev.get("kind") == "artifact_ready":
            self.artifact_announcements.append(ev)
        # Final assistant prose, useful for follow-up parsing.
        if "text" in ev and isinstance(ev.get("text"), str):
            self.final_assistant_text.append(ev["text"])
        if ev.get("kind") == "complete" or "timing" in ev:
            self.saw_complete = True

    def llm_declared_paths(self, *needles: str) -> list[dict[str, Any]]:
        """Return every LLM-declared delivery matching the needles.

        Looks across done.files entries AND artifact_ready events; both are
        LLM-emitted delivery declarations. We do NOT filesystem-search.
        """
        wants = [n.lower() for n in needles if n]
        results: list[dict[str, Any]] = []

        def _matches(entry: dict[str, Any]) -> bool:
            if not wants:
                return True
            hay = " ".join(
                str(entry.get(k) or "").lower()
                for k in ("name", "path", "rel_path", "local_path", "url")
            )
            return any(w in hay for w in wants)

        for entry in self.done_files:
            if _matches(entry):
                results.append(entry)
        for ev in self.artifact_announcements:
            entry = {
                "name": ev.get("path"),
                "rel_path": ev.get("path"),
                "artifact_type": ev.get("artifact_type"),
                "artifact_id": ev.get("artifact_id"),
                "source": "artifact_ready",
            }
            if _matches(entry):
                results.append(entry)
        return results

    def delivered_paths(self) -> list[str]:
        out: list[str] = []
        for entry in self.done_files:
            path = entry.get("local_path") or entry.get("rel_path") or entry.get("name")
            if path:
                out.append(str(path))
        for ev in self.artifact_announcements:
            p = ev.get("path")
            if p:
                out.append(str(p))
        return out

    def matched(self, *needles: str) -> list[dict[str, Any]]:
        """Backward-compat: alias for ``llm_declared_paths``."""
        return self.llm_declared_paths(*needles)


def parse_followup_answer(answer_text: str, expected: str) -> dict[str, Any]:
    """Interpret the LLM's follow-up answer.

    The follow-up prompt asks for either a relative path (when delivered) or
    the literal phrase ``not delivered``. This parser respects that contract.

    Returns ``{"declared": bool, "path": str|None, "negative": bool}``.
    """
    text = (answer_text or "").strip()
    low = text.lower()
    expected_low = (expected or "").lower()
    negative_markers = (
        "not delivered",
        "not_delivered",
        "未交付",
        "未完成",
        "没有交付",
        "没产出",
        "未生成",
    )
    if any(m in low for m in negative_markers):
        return {"declared": False, "path": None, "negative": True}
    # Look for the expected token in the text — either the basename or any
    # path-like substring containing it.
    if expected_low and expected_low in low:
        # Try to extract the first path-shaped token containing it.
        import re

        for m in re.finditer(r"[\w./\\\-]+", text):
            tok = m.group(0)
            if expected_low in tok.lower():
                return {"declared": True, "path": tok, "negative": False}
        # Fall through: expected name was mentioned but not as a path token.
        return {"declared": True, "path": text, "negative": False}
    return {"declared": False, "path": None, "negative": False}


def ask_followup_about_delivery(
    api_url: str,
    *,
    archive_id: str,
    user_id: str,
    current_dir: str,
    expected: str,
    persona_id: str = "environment",
    timeout: int = 60,
    wall_cap_sec: int = 240,
    stall_cap_sec: int = 60,
) -> dict[str, Any]:
    """Post a follow-up turn to the same archive asking about delivery.

    Returns ``{"answer_text": str, "files": [...], "raw_complete": dict}``.
    The driver inspects ``files`` (LLM's authoritative list) and the prose
    answer; it does NOT touch the filesystem.
    """
    body = {
        "user_id": user_id,
        "user_name": "LongTest",
        "message": (
            f"Status check: did you actually deliver `{expected}` in the previous turn? "
            "If yes, reply with the relative path you wrote it to (no extra prose). "
            "If you did not finish, say 'not delivered' and one short sentence on why. "
            "Do not start new work; this is a status check only."
            f"\n\n中文：你上一轮是否已交付 `{expected}`？已交付请只回相对路径；未完成请回 \"not delivered\" 并一句话说明原因。不要开始新工作。"
        ),
        "current_dir": current_dir,
        "persona_id": persona_id,
        "archive_id": archive_id,
        "client_msg_id": f"{archive_id}-followup-{int(time.time())}",
    }
    req = urllib.request.Request(
        api_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    capture = DoneCapture()
    tokens: list[str] = []
    raw_complete: dict[str, Any] = {}
    current_event = ["message"]

    def _on_text(text: str, _t: float) -> None:
        if text == "":
            current_event[0] = "message"
            return
        if text.startswith("event: "):
            current_event[0] = text[len("event: "):].strip()
            return
        if text.startswith("data: "):
            capture.feed_line(text)
            if current_event[0] == "token":
                try:
                    payload = json.loads(text[6:])
                    tokens.append(str(payload.get("text") or ""))
                except Exception:
                    pass
            elif current_event[0] == "complete":
                try:
                    raw_complete.update(json.loads(text[6:]))
                except Exception:
                    pass

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            stream = read_sse_with_stall_cap(
                resp,
                wall_cap_sec=wall_cap_sec,
                stall_cap_sec=stall_cap_sec,
                on_text=_on_text,
            )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "answer_text": "",
            "files": [],
            "raw_complete": {},
        }

    return {
        "ok": True,
        "answer_text": "".join(tokens).strip(),
        "files": capture.done_files,
        "raw_complete": raw_complete,
        "elapsed_sec": stream.elapsed_sec,
        "truncated_wall_cap": stream.truncated_wall_cap,
        "truncated_stall_cap": stream.truncated_stall_cap,
    }
