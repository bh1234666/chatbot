"""Generate prompt cache reports from debug logs.

The report compares local prompt-shape estimates with provider usage cache
statistics when both are present. It is intentionally offline and does not call
LLMs.

缓存报告离线解析 debug 日志，不调用模型。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import fnmatch
import json
from pathlib import Path
import re
from statistics import mean
from typing import Iterable


_HEADER_RE = re.compile(
    r"^\[(?P<time>[^\]]+)\]\s+\[(?P<trace>[^\]]*)\]\s+\[(?P<category>[^\]]+)\]\s*(?P<msg>.*)$"
)

_SHAPE_RE = re.compile(
    r"(?P<label>[^:]+):\s+model=(?P<model>\S+)\s+msgs=(?P<msgs>\d+)\s+tools=(?P<tools>\d+)\s+"
    r"static=(?P<static>\d+)\s+dynamic=(?P<dynamic>\d+)\s+cacheable_prefix=(?P<prefix>\d+)\s+"
    r"system=(?P<system>[0-9a-f]+)\s+tools_hash=(?P<tools_hash>[0-9a-f]+)"
)

_CACHE_RE = re.compile(
    r"P49\s+\[(?P<tag>[^\]]+)\]:\s+model=(?P<model>\S+)\s+prompt=(?P<prompt>\d+)\s+"
    r"completion=(?P<completion>\d+)\s+cache_hit=(?P<hit>\d+)\s+cache_miss=(?P<miss>\d+)\s+"
    r"hit_rate=(?P<rate>\d+)%"
)


@dataclass(frozen=True)
class PromptShape:
    trace: str
    label: str
    tag_hint: str
    model: str
    messages: int
    tools: int
    static_bytes: int
    dynamic_bytes: int
    cacheable_prefix_bytes: int
    system_hash: str
    tool_schema_hash: str
    system_sections: tuple[dict[str, object], ...] = ()
    message_sections: tuple[dict[str, object], ...] = ()
    hash_chain: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class CacheStats:
    time_text: str
    timestamp: float | None
    trace: str
    tag: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    hit_rate_percent: int


@dataclass(frozen=True)
class RouteEvent:
    time_text: str
    timestamp: float | None
    trace: str
    category: str
    message: str


@dataclass(frozen=True)
class CacheReport:
    shapes: tuple[PromptShape, ...]
    stats: tuple[CacheStats, ...]
    route_events: tuple[RouteEvent, ...] = ()


def parse_debug_log_text(text: str) -> CacheReport:
    shapes: list[PromptShape] = []
    stats: list[CacheStats] = []
    route_events: list[RouteEvent] = []
    pending_shape_index: int | None = None
    pending_payload_lines: list[str] = []

    def flush_payload() -> None:
        nonlocal pending_shape_index, pending_payload_lines
        if pending_shape_index is None or not pending_payload_lines:
            pending_shape_index = None
            pending_payload_lines = []
            return
        payload_text = "\n".join(pending_payload_lines)
        pending_shape_index_local = pending_shape_index
        pending_shape_index = None
        pending_payload_lines = []
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        system_sections = payload.get("system_sections")
        message_sections = payload.get("message_sections")
        hash_chain = payload.get("hash_chain")
        if not isinstance(system_sections, list):
            system_sections = []
        if not isinstance(message_sections, list):
            message_sections = []
        if not isinstance(hash_chain, list):
            hash_chain = []
        old = shapes[pending_shape_index_local]
        shapes[pending_shape_index_local] = PromptShape(
            trace=old.trace,
            label=old.label,
            tag_hint=old.tag_hint,
            model=old.model,
            messages=old.messages,
            tools=old.tools,
            static_bytes=old.static_bytes,
            dynamic_bytes=old.dynamic_bytes,
            cacheable_prefix_bytes=old.cacheable_prefix_bytes,
            system_hash=old.system_hash,
            tool_schema_hash=old.tool_schema_hash,
            system_sections=tuple(item for item in system_sections if isinstance(item, dict)),
            message_sections=tuple(item for item in message_sections if isinstance(item, dict)),
            hash_chain=tuple(item for item in hash_chain if isinstance(item, dict)),
        )

    for raw_line in text.splitlines():
        header = _HEADER_RE.match(raw_line)
        if not header:
            if pending_shape_index is not None and raw_line.startswith("[") and "]   " in raw_line:
                pending_payload_lines.append(raw_line.split("]   ", 1)[1])
            continue
        category = header.group("category").strip()
        raw_msg = header.group("msg").rstrip()
        msg = raw_msg.strip()
        trace = header.group("trace").strip()
        if category == "llm.prompt_cache_shape":
            match = _SHAPE_RE.search(msg)
            if not match:
                if pending_shape_index is not None and msg:
                    pending_payload_lines.append(raw_msg)
                continue
            flush_payload()
            shapes.append(
                PromptShape(
                    trace=trace,
                    label=match.group("label").strip(),
                    tag_hint=_shape_label_tag_hint(match.group("label").strip()),
                    model=match.group("model"),
                    messages=int(match.group("msgs")),
                    tools=int(match.group("tools")),
                    static_bytes=int(match.group("static")),
                    dynamic_bytes=int(match.group("dynamic")),
                    cacheable_prefix_bytes=int(match.group("prefix")),
                    system_hash=match.group("system"),
                    tool_schema_hash=match.group("tools_hash"),
                )
            )
            pending_shape_index = len(shapes) - 1
        elif category == "llm.cache_stats":
            flush_payload()
            match = _CACHE_RE.search(msg)
            if not match:
                continue
            time_text = header.group("time").strip()
            stats.append(
                CacheStats(
                    time_text=time_text,
                    timestamp=_parse_log_time_to_seconds(time_text),
                    trace=trace,
                    tag=match.group("tag"),
                    model=match.group("model"),
                    prompt_tokens=int(match.group("prompt")),
                    completion_tokens=int(match.group("completion")),
                    cache_hit_tokens=int(match.group("hit")),
                    cache_miss_tokens=int(match.group("miss")),
                    hit_rate_percent=int(match.group("rate")),
                )
            )
        elif category.startswith("round2.upgrade_") or category in {
            "delegate.helper_route",
            "llm.tools.start",
            "llm.tools.model_spec_switch",
            "llm.tools.model_switch",
            "llm.tools.reasoning_switch",
        }:
            flush_payload()
            time_text = header.group("time").strip()
            route_events.append(
                RouteEvent(
                    time_text=time_text,
                    timestamp=_parse_log_time_to_seconds(time_text),
                    trace=trace,
                    category=category,
                    message=msg,
                )
            )
        else:
            flush_payload()
    flush_payload()
    return CacheReport(shapes=tuple(shapes), stats=tuple(stats), route_events=tuple(route_events))


def _parse_log_time_to_seconds(value: str) -> float | None:
    """Parse debug log time text into seconds within the day when possible."""
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return float(dt.hour * 3600 + dt.minute * 60 + dt.second) + dt.microsecond / 1_000_000
        except ValueError:
            continue
    return None


def parse_debug_logs(paths: Iterable[str | Path]) -> CacheReport:
    shapes: list[PromptShape] = []
    stats: list[CacheStats] = []
    route_events: list[RouteEvent] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        report = parse_debug_log_text(path.read_text(encoding="utf-8", errors="replace"))
        shapes.extend(report.shapes)
        stats.extend(report.stats)
        route_events.extend(report.route_events)
    return CacheReport(shapes=tuple(shapes), stats=tuple(stats), route_events=tuple(route_events))


def _shape_label_tag_hint(label: str) -> str:
    """Infer the nearest cache usage tag family from a prompt-shape label."""
    text = str(label or "").strip()
    if not text:
        return "unknown"
    helper_marker = ".helper."
    if helper_marker in text:
        helper_task = text.split(helper_marker, 1)[1].strip(".")
        if ".call." in helper_task:
            helper_task = helper_task.split(".call.", 1)[0].strip(".")
        return f"helper.{helper_task}" if helper_task else "helper.*"
    if text.endswith(".main") or ".main." in text:
        return "main"
    if text.endswith(".helper") or ".helper." in text:
        return "helper.*"
    if text == "chat_stream":
        return "chat_stream"
    return text.split(".", 1)[0]


def _group_shapes(shapes: Iterable[PromptShape]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[PromptShape]] = {}
    for item in shapes:
        grouped.setdefault((item.label, item.model), []).append(item)
    rows: list[dict[str, object]] = []
    for (label, model), items in sorted(grouped.items()):
        avg_static = round(mean(x.static_bytes for x in items))
        avg_dynamic = round(mean(x.dynamic_bytes for x in items))
        avg_prefix = round(mean(x.cacheable_prefix_bytes for x in items))
        avg_total = avg_static + avg_dynamic
        rows.append(
            {
                "label": label,
                "tag_hint": items[0].tag_hint,
                "model": model,
                "calls": len(items),
                "avg_static_bytes": avg_static,
                "avg_dynamic_bytes": avg_dynamic,
                "avg_cacheable_prefix_bytes": avg_prefix,
                "local_prefix_share_percent": round(avg_prefix * 100 / avg_total, 1) if avg_total else "n/a",
                "system_hashes": len({x.system_hash for x in items}),
                "tool_schema_hashes": len({x.tool_schema_hash for x in items}),
            }
        )
    return rows


def _group_stats(stats: Iterable[CacheStats]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[CacheStats]] = {}
    for item in stats:
        grouped.setdefault((item.tag, item.model), []).append(item)
    rows: list[dict[str, object]] = []
    for (tag, model), items in sorted(grouped.items()):
        hit = sum(x.cache_hit_tokens for x in items)
        miss = sum(x.cache_miss_tokens for x in items)
        total = hit + miss
        rows.append(
            {
                "tag": tag,
                "model": model,
                "calls": len(items),
                "prompt_tokens": sum(x.prompt_tokens for x in items),
                "completion_tokens": sum(x.completion_tokens for x in items),
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "hit_rate_percent": round(hit * 100 / total, 1) if total else "n/a",
            }
        )
    return rows


def _group_stats_by_tag(stats: Iterable[CacheStats]) -> list[dict[str, object]]:
    grouped: dict[str, list[CacheStats]] = {}
    for item in stats:
        grouped.setdefault(item.tag, []).append(item)
    rows: list[dict[str, object]] = []
    for tag, items in sorted(grouped.items()):
        hit = sum(x.cache_hit_tokens for x in items)
        miss = sum(x.cache_miss_tokens for x in items)
        total = hit + miss
        models = sorted({x.model for x in items})
        rows.append(
            {
                "tag": tag,
                "models": ",".join(models),
                "calls": len(items),
                "prompt_tokens": sum(x.prompt_tokens for x in items),
                "completion_tokens": sum(x.completion_tokens for x in items),
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "hit_rate_percent": round(hit * 100 / total, 1) if total else "n/a",
            }
        )
    return rows


def _warm_stats(stats: Iterable[CacheStats], *, skip_first: int = 2) -> tuple[CacheStats, ...]:
    """Return same-tag/model cache stats after dropping the first few cold calls.

    Provider prefix cache has an unavoidable warm-up period for a new tag/model
    family. This helper keeps the global usage rows intact while exposing a
    separate steady-state view for long tool chains.

    同一 tag/model 的前几次调用视为预热；全局统计保留，稳态统计单独展示。
    """
    grouped: dict[tuple[str, str], list[CacheStats]] = {}
    for item in stats:
        grouped.setdefault((item.tag, item.model), []).append(item)
    warm: list[CacheStats] = []
    for items in grouped.values():
        ordered = sorted(
            items,
            key=lambda item: item.timestamp if item.timestamp is not None else float("inf"),
        )
        warm.extend(ordered[max(0, skip_first):])
    return tuple(
        sorted(
            warm,
            key=lambda item: item.timestamp if item.timestamp is not None else float("inf"),
        )
    )


def _section_rows(shapes: Iterable[PromptShape]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, object]]] = {}
    for shape in shapes:
        for source, sections in (
            ("system", shape.system_sections),
            ("message", shape.message_sections),
        ):
            for section in sections:
                label = str(section.get("label") or "").strip()
                if not label:
                    continue
                grouped.setdefault((shape.label, shape.model, source, label), []).append(section)

    rows: list[dict[str, object]] = []
    for (label, model, source, section_label), items in sorted(grouped.items()):
        hashes = {str(item.get("hash") or "") for item in items if item.get("hash")}
        byte_values: list[int] = []
        for item in items:
            try:
                byte_values.append(int(item.get("bytes") or 0))
            except (TypeError, ValueError):
                pass
        rows.append(
            {
                "label": label,
                "model": model,
                "source": source,
                "section": section_label,
                "calls": len(items),
                "hashes": len(hashes),
                "avg_bytes": round(mean(byte_values)) if byte_values else 0,
            }
        )
    return rows


def _hash_chain_rows(shapes: Iterable[PromptShape]) -> list[dict[str, object]]:
    """Return hash-chain segment stability rows grouped by prompt shape."""
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for shape in shapes:
        for segment in shape.hash_chain:
            label = str(segment.get("label") or "").strip()
            if not label:
                continue
            grouped.setdefault((shape.label, shape.model, label), []).append(segment)

    rows: list[dict[str, object]] = []
    for (label, model, segment_label), items in sorted(grouped.items()):
        chain_hashes = {str(item.get("hash") or "") for item in items if item.get("hash")}
        segment_hashes = {
            str(item.get("segment_hash") or "") for item in items if item.get("segment_hash")
        }
        byte_values: list[int] = []
        for item in items:
            try:
                byte_values.append(int(item.get("bytes") or 0))
            except (TypeError, ValueError):
                pass
        rows.append(
            {
                "label": label,
                "model": model,
                "segment": segment_label,
                "calls": len(items),
                "chain_hashes": len(chain_hashes),
                "segment_hashes": len(segment_hashes),
                "avg_bytes": round(mean(byte_values)) if byte_values else 0,
            }
        )
    return rows


_PREFIX_CACHE_HASH_SEGMENTS = {"system_static", "tool_schema"}


def _prefix_hash_chain_rows(shapes: Iterable[PromptShape]) -> list[dict[str, object]]:
    """Return hash-chain rows for prefix-critical cache segments only."""
    return [
        row for row in _hash_chain_rows(shapes)
        if str(row["segment"]) in _PREFIX_CACHE_HASH_SEGMENTS
    ]


def _low_hit_diagnostic_rows(
    stats: Iterable[CacheStats],
    route_events: Iterable[RouteEvent] = (),
    shapes: Iterable[PromptShape] = (),
    *,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Return low-hit calls with same-tag/model interval and likely-cause hints."""
    stat_items = tuple(stats)
    shape_items = tuple(shapes)
    matching_shapes_by_stat = _matching_shapes_by_stat(stat_items, shape_items)
    last_seen: dict[tuple[str, str], CacheStats] = {}
    last_seen_trace_tag: dict[tuple[str, str], CacheStats] = {}
    seen_trace_tag_models: dict[tuple[str, str], set[str]] = {}
    events_by_trace: dict[str, list[RouteEvent]] = {}
    for event in route_events:
        if event.trace:
            events_by_trace.setdefault(event.trace, []).append(event)
    rows: list[dict[str, object]] = []
    for item in stat_items:
        key = (item.tag, item.model)
        previous = last_seen.get(key)
        last_seen[key] = item
        trace_tag = (item.trace, item.tag)
        previous_trace_tag = last_seen_trace_tag.get(trace_tag)
        known_trace_tag_models = seen_trace_tag_models.setdefault(trace_tag, set())
        first_in_trace_tag_model = item.model not in known_trace_tag_models
        known_trace_tag_models.add(item.model)
        last_seen_trace_tag[trace_tag] = item
        total = item.cache_hit_tokens + item.cache_miss_tokens
        if total <= 0:
            continue
        hit_rate = item.cache_hit_tokens * 100 / total
        if hit_rate >= 70.0:
            continue
        interval: float | None = None
        if previous and item.timestamp is not None and previous.timestamp is not None:
            interval = item.timestamp - previous.timestamp
            if interval < 0:
                interval = None
        route_labels = [_route_event_label(event) for event in events_by_trace.get(item.trace, [])]
        route_context = ", ".join(route_labels[:4]) or "none"
        likely_cause = _classify_low_hit_cause(
            item=item,
            previous_same_tag_model=previous,
            previous_trace_tag=previous_trace_tag,
            first_in_trace_tag_model=first_in_trace_tag_model,
            interval_seconds=interval,
            route_labels=route_labels,
        )
        likely_cause = _refine_low_hit_cause_with_shapes(
            cause=likely_cause,
            item=item,
            previous_same_tag_model=previous,
            matching_shapes_by_stat=matching_shapes_by_stat,
        )
        rows.append(
            {
                "time": item.time_text,
                "trace": item.trace,
                "tag": item.tag,
                "model": item.model,
                "hit_rate_percent": round(hit_rate, 1),
                "cache_hit_tokens": item.cache_hit_tokens,
                "cache_miss_tokens": item.cache_miss_tokens,
                "seconds_since_same_tag_model": round(interval, 1) if interval is not None else "n/a",
                "likely_cause": likely_cause,
                "route_context": route_context,
                "shape_evidence": _shape_evidence_for_stat(item, shape_items),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            float(row["hit_rate_percent"]),
            -int(row["cache_miss_tokens"]),
        ),
    )[:limit]


def _low_hit_cause_summary_rows(
    stats: Iterable[CacheStats],
    route_events: Iterable[RouteEvent] = (),
    shapes: Iterable[PromptShape] = (),
    *,
    limit: int = 30,
) -> list[dict[str, object]]:
    """Aggregate all low-hit calls by likely cause, tag, and model."""
    stat_items = tuple(stats)
    matching_shapes_by_stat = _matching_shapes_by_stat(stat_items, tuple(shapes))
    last_seen: dict[tuple[str, str], CacheStats] = {}
    last_seen_trace_tag: dict[tuple[str, str], CacheStats] = {}
    seen_trace_tag_models: dict[tuple[str, str], set[str]] = {}
    events_by_trace: dict[str, list[RouteEvent]] = {}
    for event in route_events:
        if event.trace:
            events_by_trace.setdefault(event.trace, []).append(event)

    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for item in stat_items:
        key = (item.tag, item.model)
        previous = last_seen.get(key)
        last_seen[key] = item
        trace_tag = (item.trace, item.tag)
        previous_trace_tag = last_seen_trace_tag.get(trace_tag)
        known_trace_tag_models = seen_trace_tag_models.setdefault(trace_tag, set())
        first_in_trace_tag_model = item.model not in known_trace_tag_models
        known_trace_tag_models.add(item.model)
        last_seen_trace_tag[trace_tag] = item

        total = item.cache_hit_tokens + item.cache_miss_tokens
        if total <= 0:
            continue
        hit_rate = item.cache_hit_tokens * 100 / total
        if hit_rate >= 70.0:
            continue
        interval: float | None = None
        if previous and item.timestamp is not None and previous.timestamp is not None:
            interval = item.timestamp - previous.timestamp
            if interval < 0:
                interval = None
        route_labels = [_route_event_label(event) for event in events_by_trace.get(item.trace, [])]
        cause = _classify_low_hit_cause(
            item=item,
            previous_same_tag_model=previous,
            previous_trace_tag=previous_trace_tag,
            first_in_trace_tag_model=first_in_trace_tag_model,
            interval_seconds=interval,
            route_labels=route_labels,
        )
        cause = _refine_low_hit_cause_with_shapes(
            cause=cause,
            item=item,
            previous_same_tag_model=previous,
            matching_shapes_by_stat=matching_shapes_by_stat,
        )
        row_key = (cause, item.tag, item.model)
        row = grouped.setdefault(
            row_key,
            {
                "likely_cause": cause,
                "tag": item.tag,
                "model": item.model,
                "calls": 0,
                "prompt_tokens": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
            },
        )
        row["calls"] = int(row["calls"]) + 1
        row["prompt_tokens"] = int(row["prompt_tokens"]) + item.prompt_tokens
        row["cache_hit_tokens"] = int(row["cache_hit_tokens"]) + item.cache_hit_tokens
        row["cache_miss_tokens"] = int(row["cache_miss_tokens"]) + item.cache_miss_tokens

    rows: list[dict[str, object]] = []
    for row in grouped.values():
        hit = int(row["cache_hit_tokens"])
        miss = int(row["cache_miss_tokens"])
        total = hit + miss
        rows.append(
            {
                **row,
                "hit_rate_percent": round(hit * 100 / total, 1) if total else "n/a",
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -int(row["cache_miss_tokens"]),
            str(row["likely_cause"]),
            str(row["tag"]),
            str(row["model"]),
        ),
    )[:limit]


def _classify_low_hit_cause(
    *,
    item: CacheStats,
    previous_same_tag_model: CacheStats | None,
    previous_trace_tag: CacheStats | None,
    first_in_trace_tag_model: bool,
    interval_seconds: float | None,
    route_labels: list[str],
) -> str:
    """Classify a low-hit call using only observable cache and route evidence."""
    route_changed = bool(route_labels) and any(
        label != "tools_start" for label in route_labels
    )
    trace_model_changed = (
        previous_trace_tag is not None and previous_trace_tag.model != item.model
    )
    if (route_changed or trace_model_changed) and first_in_trace_tag_model:
        return "model_switch_cold_start"
    if interval_seconds is not None and interval_seconds >= 300.0:
        return "ttl_or_long_idle"
    if previous_same_tag_model is None:
        return "first_seen_tag_model"
    if interval_seconds is not None and interval_seconds <= 10.0:
        return "short_interval_prefix_change"
    return "normal_low_hit"


def _matching_shapes_by_stat(
    stats: Iterable[CacheStats],
    shapes: Iterable[PromptShape],
) -> dict[CacheStats, tuple[PromptShape, ...]]:
    """Map each cache usage row to prompt shapes with same trace/model/tag family."""
    shape_items = tuple(shapes)
    result: dict[CacheStats, tuple[PromptShape, ...]] = {}
    for item in stats:
        result[item] = tuple(
            shape for shape in shape_items
            if shape.trace == item.trace
            and shape.model == item.model
            and _tag_matches_shape_hint(item.tag, shape.tag_hint)
        )
    return result


def _refine_low_hit_cause_with_shapes(
    *,
    cause: str,
    item: CacheStats,
    previous_same_tag_model: CacheStats | None,
    matching_shapes_by_stat: dict[CacheStats, tuple[PromptShape, ...]],
) -> str:
    """Use prompt-shape evidence to refine short-interval low-hit diagnostics."""
    if cause != "short_interval_prefix_change" or previous_same_tag_model is None:
        return cause
    current_shapes = matching_shapes_by_stat.get(item) or ()
    previous_shapes = matching_shapes_by_stat.get(previous_same_tag_model) or ()
    if not current_shapes or not previous_shapes:
        return cause
    current = current_shapes[-1]
    previous = previous_shapes[-1]
    first_changed = _first_changed_hash_chain_segment(previous, current)
    if first_changed in {"system_static", "tool_schema"}:
        return f"short_interval_stable_prefix_change:{first_changed}"
    if first_changed == "system_dynamic":
        return "short_interval_dynamic_system_change"
    if first_changed == "messages":
        return "short_interval_messages_change"
    if first_changed == "unchanged":
        return "short_interval_upstream_or_ttl"
    return cause


def _first_changed_hash_chain_segment(previous: PromptShape, current: PromptShape) -> str:
    """Return the first hash-chain segment that changed between two prompt shapes."""
    previous_chain = {
        str(item.get("label") or ""): str(item.get("hash") or "")
        for item in previous.hash_chain
        if item.get("label")
    }
    current_chain = {
        str(item.get("label") or ""): str(item.get("hash") or "")
        for item in current.hash_chain
        if item.get("label")
    }
    for label in ("system_static", "tool_schema", "system_dynamic", "messages"):
        if previous_chain.get(label) != current_chain.get(label):
            return label
    if previous_chain or current_chain:
        return "unchanged"
    if previous.system_hash != current.system_hash:
        return "system_static"
    if previous.tool_schema_hash != current.tool_schema_hash:
        return "tool_schema"
    return "unknown"


def _tag_matches_shape_hint(tag: str, tag_hint: str) -> bool:
    """Return whether a usage tag is compatible with a prompt-shape tag hint."""
    usage_tag = str(tag or "").strip()
    hint = str(tag_hint or "").strip()
    if not usage_tag or not hint:
        return False
    if hint == "helper.*":
        return usage_tag.startswith("helper.")
    if usage_tag.startswith("helper_kind."):
        # helper_kind.* usage rows are aggregate aliases emitted for the same
        # concrete helper call. They intentionally do not have separate prompt
        # shape rows; reuse any same-trace/model concrete helper shape.
        #
        # helper_kind 是同一次 helper 调用的聚合别名，共用具体 helper 的 shape 证据。
        return hint.startswith("helper.")
    if hint == "main":
        return usage_tag == "main" or usage_tag.startswith("main.")
    return usage_tag == hint or usage_tag.startswith(f"{hint}.")


def _shape_evidence_for_stat(item: CacheStats, shapes: Iterable[PromptShape]) -> str:
    """Describe whether this usage row has same-trace/model prompt-shape evidence."""
    candidates = [
        shape for shape in shapes
        if shape.trace == item.trace
        and shape.model == item.model
        and _tag_matches_shape_hint(item.tag, shape.tag_hint)
    ]
    if not candidates:
        return "shape_missing"
    labels = []
    for shape in candidates[:3]:
        label = shape.label
        if label not in labels:
            labels.append(label)
    suffix = f":{','.join(labels)}" if labels else ""
    return f"shape_seen{suffix}"


def _shape_coverage_rows(
    stats: Iterable[CacheStats],
    shapes: Iterable[PromptShape],
) -> list[dict[str, object]]:
    """Summarize how many cache usage rows have matching local prompt-shape evidence."""
    shape_items = tuple(shapes)
    grouped: dict[tuple[str, str], list[CacheStats]] = {}
    for item in stats:
        grouped.setdefault((item.tag, item.model), []).append(item)
    rows: list[dict[str, object]] = []
    for (tag, model), items in sorted(grouped.items()):
        covered = sum(
            1
            for item in items
            if _shape_evidence_for_stat(item, shape_items) != "shape_missing"
        )
        calls = len(items)
        rows.append(
            {
                "tag": tag,
                "model": model,
                "usage_calls": calls,
                "prompt_tokens": sum(item.prompt_tokens for item in items),
                "shape_matched_calls": covered,
                "coverage_percent": round(covered * 100 / calls, 1) if calls else "n/a",
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            float(row["coverage_percent"]) if row["coverage_percent"] != "n/a" else -1.0,
            -int(row["usage_calls"]),
            str(row["tag"]),
            str(row["model"]),
        ),
    )


def _model_route_diagnostic_rows(
    stats: Iterable[CacheStats],
    route_events: Iterable[RouteEvent],
    *,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Find trace/tag groups that switched models during one workflow."""
    grouped: dict[tuple[str, str], list[CacheStats]] = {}
    for item in stats:
        if not item.trace:
            continue
        grouped.setdefault((item.trace, item.tag), []).append(item)
    events_by_trace: dict[str, list[RouteEvent]] = {}
    for event in route_events:
        if event.trace:
            events_by_trace.setdefault(event.trace, []).append(event)

    rows: list[dict[str, object]] = []
    for (trace, tag), items in grouped.items():
        if len(items) < 2:
            continue
        models_in_order: list[str] = []
        switches = 0
        previous_model = ""
        for item in items:
            if item.model != previous_model:
                models_in_order.append(item.model)
                if previous_model:
                    switches += 1
                previous_model = item.model
        unique_models = sorted({item.model for item in items})
        if len(unique_models) <= 1:
            continue
        hit = sum(item.cache_hit_tokens for item in items)
        miss = sum(item.cache_miss_tokens for item in items)
        total = hit + miss
        route_event_labels = [
            _route_event_label(event) for event in events_by_trace.get(trace, [])[:4]
        ]
        rows.append(
            {
                "trace": trace,
                "tag": tag,
                "calls": len(items),
                "switches": switches,
                "models": " -> ".join(models_in_order),
                "route_events": ", ".join(route_event_labels) or "none",
                "evidence": "explained" if route_event_labels else "missing_route_event",
                "hit_rate_percent": round(hit * 100 / total, 1) if total else "n/a",
                "cache_miss_tokens": miss,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -int(row["switches"]),
            -int(row["cache_miss_tokens"]),
            str(row["trace"]),
            str(row["tag"]),
        ),
    )[:limit]


def _route_event_label(event: RouteEvent) -> str:
    """Compact route event label for Markdown diagnostics."""
    if event.category.startswith("round2.upgrade_"):
        return event.category.replace("round2.upgrade_", "upgrade:")
    if event.category == "delegate.helper_route":
        return "helper_route"
    if event.category == "llm.tools.start":
        return "tools_start"
    if event.category == "llm.tools.model_spec_switch":
        return "model_spec_switch"
    if event.category == "llm.tools.model_switch":
        return "model_switch"
    if event.category == "llm.tools.reasoning_switch":
        return "reasoning_switch"
    return event.category


def render_cache_report_markdown(
    report: CacheReport,
    *,
    reference_hit_rate_by_tag: dict[str, float] | None = None,
    reference_warm_hit_rate_by_tag: dict[str, float] | None = None,
    reference_shape_coverage_by_tag: dict[str, float] | None = None,
    long_helper_min_prompt_tokens: int = 20000,
) -> str:
    shape_rows = _group_shapes(report.shapes)
    stat_rows = _group_stats(report.stats)
    tag_stat_rows = _group_stats_by_tag(report.stats)
    warm_stat_rows = _group_stats(_warm_stats(report.stats))
    section_rows = _section_rows(report.shapes)
    unstable_section_rows = [row for row in section_rows if int(row["hashes"]) > 1]
    hash_chain_rows = _hash_chain_rows(report.shapes)
    unstable_hash_chain_rows = [
        row for row in hash_chain_rows
        if int(row["chain_hashes"]) > 1 or int(row["segment_hashes"]) > 1
    ]
    prefix_hash_chain_rows = _prefix_hash_chain_rows(report.shapes)
    unstable_prefix_hash_chain_rows = [
        row for row in prefix_hash_chain_rows
        if int(row["chain_hashes"]) > 1 or int(row["segment_hashes"]) > 1
    ]
    shape_coverage_rows = _shape_coverage_rows(report.stats, report.shapes)
    low_hit_summary_rows = _low_hit_cause_summary_rows(report.stats, report.route_events, report.shapes)
    low_hit_rows = _low_hit_diagnostic_rows(report.stats, report.route_events, report.shapes)
    route_rows = _model_route_diagnostic_rows(report.stats, report.route_events)
    reference_hit_rows = _reference_rows(
        stat_rows,
        reference_hit_rate_by_tag or {},
        value_key="hit_rate_percent",
        calls_key="calls",
        long_helper_min_prompt_tokens=long_helper_min_prompt_tokens,
    )
    reference_warm_rows = _reference_rows(
        warm_stat_rows,
        reference_warm_hit_rate_by_tag or {},
        value_key="hit_rate_percent",
        calls_key="calls",
        long_helper_min_prompt_tokens=long_helper_min_prompt_tokens,
    )
    reference_shape_rows = _reference_rows(
        shape_coverage_rows,
        reference_shape_coverage_by_tag or {},
        value_key="coverage_percent",
        calls_key="usage_calls",
        long_helper_min_prompt_tokens=long_helper_min_prompt_tokens,
    )
    lines: list[str] = [
        "# Prompt Cache Report",
        "",
        "This report is generated from debug logs. Shape rows are local prompt-structure estimates; usage rows are provider cache usage when the upstream returned `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`.",
        "",
        "缓存报告：Shape 是本地输入结构估算，Usage 是上游返回的真实缓存 token 统计。",
        "",
        "## Summary",
        "",
        f"- Prompt shape events: {len(report.shapes)}",
        f"- Cache usage events: {len(report.stats)}",
        f"- Shape groups: {len(shape_rows)}",
        f"- Usage groups: {len(stat_rows)}",
        f"- Usage tag groups: {len(tag_stat_rows)}",
        f"- Warm usage groups (skip first 2 same tag/model calls): {len(warm_stat_rows)}",
        f"- Section groups: {len(section_rows)}",
        f"- Unstable section groups: {len(unstable_section_rows)}",
        f"- Hash chain groups: {len(hash_chain_rows)}",
        f"- Unstable hash chain groups: {len(unstable_hash_chain_rows)}",
        f"- Prefix hash chain groups: {len(prefix_hash_chain_rows)}",
        f"- Unstable prefix hash chain groups: {len(unstable_prefix_hash_chain_rows)}",
        f"- Shape coverage groups: {len(shape_coverage_rows)}",
        f"- Low-hit cause groups: {len(low_hit_summary_rows)}",
        f"- Low-hit diagnostic rows: {len(low_hit_rows)}",
        f"- Model route diagnostic rows: {len(route_rows)}",
        "",
    ]

    if not report.stats:
        lines.extend(
            [
                "## Provider Usage Evidence Missing",
                "",
                "This log has prompt-shape events but no provider cache-usage events. It can verify local prefix structure, section stability, and hash-chain stability, but it cannot prove the real upstream cache hit rate or convergence plateau.",
                "",
                "缺少上游 usage 统计时，只能证明本地前缀结构稳定，不能证明真实命中率或收敛平台期。",
                "",
            ]
        )

    lines.extend(
        [
            "## Prompt Shape Estimates",
            "",
            "`local prefix share` is the byte share of the locally classified stable system/tool prefix. It is a structure diagnostic, not a provider-cache upper bound: providers may also reuse repeated dynamic prefixes inside one long trace, while cold starts, TTL, model switches, or upstream cache pressure can keep real hit rate below this share.",
            "",
            "local prefix share 是本地稳定 system/tool 前缀占比，用于诊断结构；它不是上游命中率上限或下限。",
            "",
            "| label | tag hint | model | calls | avg static bytes | avg dynamic bytes | avg cacheable prefix bytes | local prefix share | system hashes | tool hashes |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    if shape_rows:
        for row in shape_rows:
            lines.append(
                f"| {row['label']} | {row['tag_hint']} | {row['model']} | {row['calls']} | {row['avg_static_bytes']} | "
                f"{row['avg_dynamic_bytes']} | {row['avg_cacheable_prefix_bytes']} | "
                f"{row['local_prefix_share_percent']}% | "
                f"{row['system_hashes']} | {row['tool_schema_hashes']} |"
            )
    else:
        lines.append("| (none) |  |  | 0 | 0 | 0 | 0 | n/a | 0 | 0 |")

    lines.extend(
        [
            "",
            "## Section Stability",
            "",
            "| label | model | source | section | calls | hashes | avg bytes |",
            "|---|---:|---|---|---:|---:|---:|",
        ]
    )
    if unstable_section_rows:
        for row in unstable_section_rows:
            lines.append(
                f"| {row['label']} | {row['model']} | {row['source']} | {row['section']} | "
                f"{row['calls']} | {row['hashes']} | {row['avg_bytes']} |"
            )
    elif section_rows:
        lines.append("| (all stable) |  |  |  | 0 | 0 | 0 |")
    else:
        lines.append("| (no section payloads) |  |  |  | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "## Hash Chain Stability",
            "",
            "| label | model | segment | calls | chain hashes | segment hashes | avg bytes |",
            "|---|---:|---|---:|---:|---:|---:|",
        ]
    )
    if unstable_hash_chain_rows:
        for row in unstable_hash_chain_rows:
            lines.append(
                f"| {row['label']} | {row['model']} | {row['segment']} | {row['calls']} | "
                f"{row['chain_hashes']} | {row['segment_hashes']} | {row['avg_bytes']} |"
            )
    elif hash_chain_rows:
        lines.append("| (all stable) |  |  | 0 | 0 | 0 | 0 |")
    else:
        lines.append("| (no hash-chain payloads) |  |  | 0 | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "## Prefix Hash Chain Stability",
            "",
            "This table only checks `system_static` and `tool_schema`, the prefix-critical segments for provider cache reuse. Dynamic system or message changes remain visible in the full hash-chain table above but are separated from prefix regressions here.",
            "",
            "本表只看 system_static/tool_schema 前缀关键段，动态上下文变化保留在上方完整表中。",
            "",
            "| label | model | segment | calls | chain hashes | segment hashes | avg bytes |",
            "|---|---:|---|---:|---:|---:|---:|",
        ]
    )
    if unstable_prefix_hash_chain_rows:
        for row in unstable_prefix_hash_chain_rows:
            lines.append(
                f"| {row['label']} | {row['model']} | {row['segment']} | {row['calls']} | "
                f"{row['chain_hashes']} | {row['segment_hashes']} | {row['avg_bytes']} |"
            )
    elif prefix_hash_chain_rows:
        lines.append("| (all prefix-critical segments stable) |  |  | 0 | 0 | 0 | 0 |")
    else:
        lines.append("| (no prefix hash-chain payloads) |  |  | 0 | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "## Shape Coverage",
            "",
            "| tag | model | usage calls | shape matched calls | coverage |",
            "|---|---|---:|---:|---:|",
        ]
    )
    if shape_coverage_rows:
        for row in shape_coverage_rows[:40]:
            lines.append(
                f"| {row['tag']} | {row['model']} | {row['usage_calls']} | "
                f"{row['shape_matched_calls']} | {row['coverage_percent']}% |"
            )
    else:
        lines.append("| (no usage rows) |  | 0 | 0 | n/a |")

    lines.extend(
        [
            "",
            "## Provider Cache Usage",
            "",
            "| tag | model | calls | prompt tokens | completion tokens | cache hit | cache miss | hit rate |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    if stat_rows:
        for row in stat_rows:
            lines.append(
                f"| {row['tag']} | {row['model']} | {row['calls']} | {row['prompt_tokens']} | "
                f"{row['completion_tokens']} | {row['cache_hit_tokens']} | {row['cache_miss_tokens']} | "
                f"{row['hit_rate_percent']}% |"
            )
    else:
        lines.append("| (none) |  | 0 | 0 | 0 | 0 | 0 | n/a |")

    lines.extend(
        [
            "",
            "## Provider Cache Usage By Tag",
            "",
            "| tag | models | calls | prompt tokens | completion tokens | cache hit | cache miss | hit rate |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    if tag_stat_rows:
        for row in tag_stat_rows:
            lines.append(
                f"| {row['tag']} | {row['models']} | {row['calls']} | {row['prompt_tokens']} | "
                f"{row['completion_tokens']} | {row['cache_hit_tokens']} | {row['cache_miss_tokens']} | "
                f"{row['hit_rate_percent']}% |"
            )
    else:
        lines.append("| (none) |  | 0 | 0 | 0 | 0 | 0 | n/a |")

    lines.extend(
        [
            "",
            "## Warm Provider Cache Usage",
            "",
            "This table drops the first 2 calls for each same tag/model group. It measures steady-state prefix reuse without hiding cold-start cost from the global tables above.",
            "",
            "稳态表跳过同一 tag/model 前 2 次调用，用于观察长链稳定命中；冷启动成本仍保留在上方全局表。",
            "",
            "| tag | model | warm calls | prompt tokens | completion tokens | cache hit | cache miss | hit rate |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    if warm_stat_rows:
        for row in warm_stat_rows:
            lines.append(
                f"| {row['tag']} | {row['model']} | {row['calls']} | {row['prompt_tokens']} | "
                f"{row['completion_tokens']} | {row['cache_hit_tokens']} | {row['cache_miss_tokens']} | "
                f"{row['hit_rate_percent']}% |"
            )
    else:
        lines.append("| (no warm rows after skipping first 2 calls) |  | 0 | 0 | 0 | 0 | 0 | n/a |")

    if reference_hit_rows or reference_warm_rows or reference_shape_rows:
        lines.extend(
            [
                "",
                "## Reference Convergence Lines",
                "",
                "These are reference observation lines from the baseline file. They do not change the report exit code. Interpret them across repeated representative runs and use the plateau trend, not a fixed percentage, as the convergence signal.",
                "",
                "参考线只用于观察多轮代表性测试的平台期趋势，不作为固定目标或硬失败门禁。",
                "",
            ]
        )
        if reference_hit_rows:
            lines.extend(
                [
                    "### Global Hit-Rate References",
                    "",
                    "| scope | reference | observed | delta | calls | status |",
                    "|---|---:|---:|---:|---:|---|",
                ]
            )
            for row in reference_hit_rows:
                observed = row["observed"]
                delta = row["delta"]
                observed_text = f"{observed}%" if isinstance(observed, float) else str(observed)
                delta_text = f"{delta}%" if isinstance(delta, float) else str(delta)
                lines.append(
                    f"| {row['scope']} | {row['reference']}% | {observed_text} | "
                    f"{delta_text} | {row['calls']} | {row['status']} |"
                )
            lines.append("")
        if reference_warm_rows:
            lines.extend(
                [
                    "### Warm Hit-Rate References",
                    "",
                    "| scope | reference | observed | delta | calls | status |",
                    "|---|---:|---:|---:|---:|---|",
                ]
            )
            for row in reference_warm_rows:
                observed = row["observed"]
                delta = row["delta"]
                observed_text = f"{observed}%" if isinstance(observed, float) else str(observed)
                delta_text = f"{delta}%" if isinstance(delta, float) else str(delta)
                lines.append(
                    f"| {row['scope']} | {row['reference']}% | {observed_text} | "
                    f"{delta_text} | {row['calls']} | {row['status']} |"
                )
            lines.append("")
        if reference_shape_rows:
            lines.extend(
                [
                    "### Shape Coverage References",
                    "",
                    "| scope | reference | observed | delta | calls | status |",
                    "|---|---:|---:|---:|---:|---|",
                ]
            )
            for row in reference_shape_rows:
                observed = row["observed"]
                delta = row["delta"]
                observed_text = f"{observed}%" if isinstance(observed, float) else str(observed)
                delta_text = f"{delta}%" if isinstance(delta, float) else str(delta)
                lines.append(
                    f"| {row['scope']} | {row['reference']}% | {observed_text} | "
                    f"{delta_text} | {row['calls']} | {row['status']} |"
                )

    lines.extend(
        [
            "",
            "## Model Route Diagnostics",
            "",
            "| trace | tag | calls | model switches | model sequence | route events | evidence | hit rate | cache miss |",
            "|---|---|---:|---:|---|---|---|---:|---:|",
        ]
    )
    if route_rows:
        for row in route_rows:
            lines.append(
                f"| {row['trace']} | {row['tag']} | {row['calls']} | {row['switches']} | "
                f"{row['models']} | {row['route_events']} | {row['evidence']} | {row['hit_rate_percent']}% | "
                f"{row['cache_miss_tokens']} |"
            )
    else:
        lines.append("| (no same-trace tag model switches) |  | 0 | 0 |  |  |  | n/a | 0 |")

    lines.extend(
        [
            "",
            "## Low-Hit Cause Summary",
            "",
            "| likely cause | tag | model | calls | prompt tokens | cache hit | cache miss | hit rate |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    if low_hit_summary_rows:
        for row in low_hit_summary_rows:
            lines.append(
                f"| {row['likely_cause']} | {row['tag']} | {row['model']} | {row['calls']} | "
                f"{row['prompt_tokens']} | {row['cache_hit_tokens']} | {row['cache_miss_tokens']} | "
                f"{row['hit_rate_percent']}% |"
            )
    else:
        lines.append("| (none below 70%) |  |  | 0 | 0 | 0 | 0 | n/a |")

    lines.extend(
        [
            "",
            "## Low-Hit Call Diagnostics",
            "",
            "| time | trace | tag | model | hit rate | hit | miss | seconds since same tag/model | likely cause | route context | shape evidence |",
            "|---|---|---|---|---:|---:|---:|---:|---|---|---|",
        ]
    )
    if low_hit_rows:
        for row in low_hit_rows:
            lines.append(
                f"| {row['time']} | {row['trace']} | {row['tag']} | {row['model']} | "
                f"{row['hit_rate_percent']}% | {row['cache_hit_tokens']} | {row['cache_miss_tokens']} | "
                f"{row['seconds_since_same_tag_model']} | {row['likely_cause']} | {row['route_context']} | "
                f"{row['shape_evidence']} |"
            )
    else:
        lines.append("| (none below 70%) |  |  |  | n/a | 0 | 0 | n/a |  |  |  |")

    lines.extend(
        [
            "",
            "## Diagnostics",
            "",
            "- A shape group with many `system hashes` usually means stable system content is changing.",
            "- A shape group with many `tool hashes` usually means tool schema order or content is changing.",
            "- Section Stability lists concrete system/user sections whose hash changed within the same call shape.",
            "- Hash Chain Stability shows the first cumulative prefix segment that changed: system_static, tool_schema, system_dynamic, then messages.",
            "- Shape Coverage shows whether provider cache usage rows have matching local prompt-shape evidence. Low coverage means the report cannot yet explain prefix changes for that tag.",
            "- Model Route Diagnostics lists same-trace tag groups that crossed model branches. `missing_route_event` means the cache stats show a model switch but the log did not contain a matching upgrade/model-switch event.",
            "- Low-Hit Cause Summary ranks aggregate miss volume by likely cause and tag/model so optimization work starts where it can remove the most miss tokens.",
            "- Low-Hit Call Diagnostics classifies likely causes from observable evidence: first seen tag/model, model switch cold start, long idle or TTL, short-interval prefix change, or normal low-hit residue.",
            "- High local cacheable prefix bytes with low provider hit rate can indicate model routing changes, cache TTL expiry, or upstream cache pressure.",
            "- Gate hit rate with `--min-hit-rate tag=percent`; gate shape evidence with `--min-shape-coverage tag=percent`; both support `tag|model`. Gate hash-chain stability with `--max-unstable-hash-chain label=count`.",
            "",
            "诊断：system/tool hash 数量过多表示前缀抖动；真实命中低时优先查看低命中原因、模型路由、TTL 与上游缓存压力；可用 shape coverage 与 hash-chain 门禁确保报告有足够证据且稳定。",
            "",
        ]
    )
    return "\n".join(lines)


def _row_average_prompt_tokens(row: dict[str, object]) -> float:
    calls = int(row.get("calls", row.get("usage_calls", 0)) or 0)
    prompt_tokens = int(row.get("prompt_tokens", 0) or 0)
    return prompt_tokens / calls if calls else 0.0


def _gate_tag_matches(tag: str, pattern: str, row: dict[str, object], *, long_helper_min_prompt_tokens: int) -> bool:
    if pattern == "main":
        return tag == "main" or tag.startswith("main.")
    if pattern in {"helper.*", "helpers"}:
        return tag.startswith("helper.")
    if pattern in {"long_helper", "long_helper.*", "helper.long"}:
        return tag.startswith("helper.") and _row_average_prompt_tokens(row) >= long_helper_min_prompt_tokens
    if pattern.endswith(".*"):
        return tag.startswith(pattern[:-1])
    return tag == pattern


def _select_gate_rows(
    rows: Iterable[dict[str, object]],
    tag_expr: str,
    *,
    long_helper_min_prompt_tokens: int,
) -> list[dict[str, object]]:
    """Select rows for gate expressions.

    Supported forms:
    - `main`: exact tag
    - `main|model`: exact tag and model
    - `helper.*`: all helper tags
    - `long_helper`: helper tags whose average prompt tokens per call pass the threshold

    门禁表达式支持精确 tag、tag|model、helper.* 聚合和长 helper 聚合。
    """
    text = str(tag_expr or "").strip()
    if not text:
        return []
    if "|" in text:
        tag_pattern, model = [part.strip() for part in text.split("|", 1)]
    else:
        tag_pattern, model = text, ""
    selected: list[dict[str, object]] = []
    for row in rows:
        tag = str(row.get("tag", ""))
        row_model = str(row.get("model", ""))
        if model and row_model != model:
            continue
        if _gate_tag_matches(
            tag,
            tag_pattern,
            row,
            long_helper_min_prompt_tokens=long_helper_min_prompt_tokens,
        ):
            selected.append(row)
    return selected


def _reference_rows(
    rows: Iterable[dict[str, object]],
    references: dict[str, float],
    *,
    value_key: str,
    calls_key: str,
    long_helper_min_prompt_tokens: int,
) -> list[dict[str, object]]:
    """Summarize reference-only convergence lines without creating failures.

    Reference rows are observability data. They help compare repeated long
    tests against a reference line, but they are not pass/fail gates.

    reference 只用于观察收敛趋势，不参与失败判定。
    """
    source_rows = list(rows)
    out: list[dict[str, object]] = []
    for tag_expr, reference_value in references.items():
        candidates = _select_gate_rows(
            source_rows,
            tag_expr,
            long_helper_min_prompt_tokens=long_helper_min_prompt_tokens,
        )
        if not candidates:
            out.append({
                "scope": tag_expr,
                "reference": float(reference_value),
                "observed": "n/a",
                "delta": "n/a",
                "calls": 0,
                "status": "missing",
            })
            continue
        if value_key == "hit_rate_percent":
            hit = sum(int(row["cache_hit_tokens"]) for row in candidates)
            miss = sum(int(row["cache_miss_tokens"]) for row in candidates)
            total = hit + miss
            observed: float | str = round(hit * 100 / total, 1) if total else "n/a"
        elif value_key == "coverage_percent":
            calls = sum(int(row["usage_calls"]) for row in candidates)
            covered = sum(int(row["shape_matched_calls"]) for row in candidates)
            observed = round(covered * 100 / calls, 1) if calls else "n/a"
        else:
            numeric = [
                float(row[value_key])
                for row in candidates
                if row.get(value_key) not in (None, "n/a")
            ]
            observed = round(mean(numeric), 1) if numeric else "n/a"
        if isinstance(observed, float):
            delta: float | str = round(observed - float(reference_value), 1)
            status = "at_or_above_reference" if observed >= float(reference_value) else "below_reference"
        else:
            delta = "n/a"
            status = "missing"
        out.append({
            "scope": tag_expr,
            "reference": float(reference_value),
            "observed": observed,
            "delta": delta,
            "calls": sum(int(row.get(calls_key, 0) or 0) for row in candidates),
            "status": status,
        })
    return out


def evaluate_hit_rate_gate(
    report: CacheReport,
    *,
    minimum_by_tag: dict[str, float],
    long_helper_min_prompt_tokens: int = 20000,
) -> list[str]:
    """Return failed cache hit-rate gate messages."""
    rows = _group_stats(report.stats)

    failures: list[str] = []
    for tag_expr, minimum in minimum_by_tag.items():
        candidates = _select_gate_rows(
            rows,
            tag_expr,
            long_helper_min_prompt_tokens=long_helper_min_prompt_tokens,
        )
        if not candidates:
            failures.append(f"missing cache stats for {tag_expr}")
            continue
        hit = sum(int(row["cache_hit_tokens"]) for row in candidates)
        miss = sum(int(row["cache_miss_tokens"]) for row in candidates)
        total = hit + miss
        rate = hit * 100 / total if total else 0.0
        if rate < minimum:
            failures.append(f"{tag_expr} hit_rate {rate:.1f}% < {minimum:.1f}%")
    return failures


def evaluate_warm_hit_rate_gate(
    report: CacheReport,
    *,
    minimum_by_tag: dict[str, float],
    long_helper_min_prompt_tokens: int = 20000,
    skip_first: int = 2,
) -> list[str]:
    """Return failed steady-state cache hit-rate gate messages."""
    rows = _group_stats(_warm_stats(report.stats, skip_first=skip_first))

    failures: list[str] = []
    for tag_expr, minimum in minimum_by_tag.items():
        candidates = _select_gate_rows(
            rows,
            tag_expr,
            long_helper_min_prompt_tokens=long_helper_min_prompt_tokens,
        )
        if not candidates:
            failures.append(f"missing warm cache stats for {tag_expr}")
            continue
        hit = sum(int(row["cache_hit_tokens"]) for row in candidates)
        miss = sum(int(row["cache_miss_tokens"]) for row in candidates)
        total = hit + miss
        rate = hit * 100 / total if total else 0.0
        if rate < minimum:
            failures.append(f"{tag_expr} warm_hit_rate {rate:.1f}% < {minimum:.1f}%")
    return failures


def evaluate_shape_coverage_gate(
    report: CacheReport,
    *,
    minimum_by_tag: dict[str, float],
    long_helper_min_prompt_tokens: int = 20000,
) -> list[str]:
    """Return failed prompt-shape coverage gate messages."""
    rows = _shape_coverage_rows(report.stats, report.shapes)

    failures: list[str] = []
    for tag_expr, minimum in minimum_by_tag.items():
        candidates = _select_gate_rows(
            rows,
            tag_expr,
            long_helper_min_prompt_tokens=long_helper_min_prompt_tokens,
        )
        if not candidates:
            failures.append(f"missing shape coverage stats for {tag_expr}")
            continue
        calls = sum(int(row["usage_calls"]) for row in candidates)
        covered = sum(int(row["shape_matched_calls"]) for row in candidates)
        coverage = covered * 100 / calls if calls else 0.0
        if coverage < minimum:
            failures.append(f"{tag_expr} shape_coverage {coverage:.1f}% < {minimum:.1f}%")
    return failures


def evaluate_hash_chain_stability_gate(
    report: CacheReport,
    *,
    maximum_unstable_by_label: dict[str, float],
) -> list[str]:
    """Return failed hash-chain stability gate messages."""
    rows = _hash_chain_rows(report.shapes)
    unstable_rows = [
        row for row in rows
        if int(row["chain_hashes"]) > 1 or int(row["segment_hashes"]) > 1
    ]
    by_label_model: dict[tuple[str, str], list[dict[str, object]]] = {}
    by_label: dict[str, list[dict[str, object]]] = {}
    for row in unstable_rows:
        label = str(row["label"])
        model = str(row["model"])
        by_label_model.setdefault((label, model), []).append(row)
        by_label.setdefault(label, []).append(row)

    failures: list[str] = []
    for label_expr, maximum in maximum_unstable_by_label.items():
        if "|" in label_expr:
            label, model = label_expr.split("|", 1)
            candidates = by_label_model.get((label, model), [])
        else:
            candidates = by_label.get(label_expr, [])
        count = len(candidates)
        if count > maximum:
            failures.append(
                f"{label_expr} unstable_hash_chain_groups {count} > {maximum:.0f}"
            )
    return failures


def _gate_label_matches(label: str, pattern: str) -> bool:
    text = str(pattern or "").strip()
    if not text:
        return False
    if any(ch in text for ch in "*?["):
        return fnmatch.fnmatchcase(label, text)
    if text.endswith(".*"):
        return label.startswith(text[:-1])
    return label == text


def evaluate_prefix_hash_chain_stability_gate(
    report: CacheReport,
    *,
    maximum_unstable_by_label: dict[str, float],
) -> list[str]:
    """Return failed prefix-cache hash-chain stability gate messages.

    This gate checks only prefix-critical segments: system_static and
    tool_schema. Dynamic user messages remain visible in the full hash-chain
    report, but they are not prefix-cache regressions by themselves.

    前缀门禁只检查 system_static/tool_schema，动态 user 内容不计入失败。
    """
    rows = _hash_chain_rows(report.shapes)
    failures: list[str] = []
    for label_expr, maximum in maximum_unstable_by_label.items():
        if "|" in label_expr:
            label_pattern, model = [part.strip() for part in label_expr.split("|", 1)]
        else:
            label_pattern, model = str(label_expr).strip(), ""
        candidates: list[dict[str, object]] = []
        for row in rows:
            label = str(row["label"])
            row_model = str(row["model"])
            segment = str(row["segment"])
            if model and row_model != model:
                continue
            if segment not in _PREFIX_CACHE_HASH_SEGMENTS:
                continue
            if not _gate_label_matches(label, label_pattern):
                continue
            if int(row["chain_hashes"]) > 1 or int(row["segment_hashes"]) > 1:
                candidates.append(row)
        count = len(candidates)
        if count > maximum:
            failures.append(
                f"{label_expr} unstable_prefix_hash_chain_groups {count} > {maximum:.0f}"
            )
    return failures


def load_cache_gate_baseline(path: str | Path) -> dict[str, float]:
    """Load cache hit-rate gates from a JSON baseline file.

    Expected shape:
    {
      "minimum_hit_rate_by_tag": {
        "main": 85,
        "main|deepseek-v4-pro": 80,
        "helper_kind.read": 80
      }
    }

    基线文件声明 tag 或 tag|model 到最低命中率百分比的映射。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    structured_keys = {
        "description",
        "long_helper_min_prompt_tokens",
        "minimum_hit_rate_by_tag",
        "minimum_warm_hit_rate_by_tag",
        "minimum_shape_coverage_by_tag",
        "reference_hit_rate_by_tag",
        "reference_warm_hit_rate_by_tag",
        "reference_shape_coverage_by_tag",
        "maximum_unstable_hash_chain_groups_by_label",
        "maximum_unstable_prefix_hash_chain_groups_by_label",
    }
    if isinstance(raw, dict) and "minimum_hit_rate_by_tag" in raw:
        values = raw.get("minimum_hit_rate_by_tag", {})
    elif isinstance(raw, dict) and any(key in raw for key in structured_keys):
        values = {}
    else:
        values = raw
    if not isinstance(values, dict):
        raise ValueError("cache baseline must be an object or contain minimum_hit_rate_by_tag object")
    result: dict[str, float] = {}
    for key, value in values.items():
        tag = str(key).strip()
        if not tag:
            raise ValueError("cache baseline contains empty tag")
        result[tag] = float(value)
    return result


def load_warm_cache_gate_baseline(path: str | Path) -> dict[str, float]:
    """Load steady-state cache hit-rate gates from a JSON baseline file.

    Expected shape:
    {
      "minimum_warm_hit_rate_by_tag": {
        "main": 99,
        "helper.*": 99,
        "long_helper": 99
      }
    }

    Only `minimum_warm_hit_rate_by_tag` is a hard gate. Reference-only
    convergence references use `reference_warm_hit_rate_by_tag` and are reported
    outside this loader.

    只有 minimum 字段是硬门禁；reference 字段只作收敛观察。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    values = raw.get("minimum_warm_hit_rate_by_tag", {})
    if not isinstance(values, dict):
        raise ValueError("cache baseline minimum_warm_hit_rate_by_tag must be an object")
    result: dict[str, float] = {}
    for key, value in values.items():
        tag = str(key).strip()
        if not tag:
            raise ValueError("warm cache baseline contains empty tag")
        result[tag] = float(value)
    return result


def load_shape_coverage_gate_baseline(path: str | Path) -> dict[str, float]:
    """Load prompt-shape coverage gates from a JSON baseline file.

    Expected shape:
    {
      "minimum_shape_coverage_by_tag": {
        "main": 95,
        "helper.read_docs|deepseek-v4-pro": 90
      }
    }

    Only `minimum_shape_coverage_by_tag` is a hard gate. Reference-only
    convergence references use `reference_shape_coverage_by_tag` and are reported
    outside this loader.

    只有 minimum 字段是硬门禁；reference 字段只作收敛观察。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    values = raw.get("minimum_shape_coverage_by_tag", {})
    if not isinstance(values, dict):
        raise ValueError("cache baseline minimum_shape_coverage_by_tag must be an object")
    result: dict[str, float] = {}
    for key, value in values.items():
        tag = str(key).strip()
        if not tag:
            raise ValueError("shape coverage baseline contains empty tag")
        result[tag] = float(value)
    return result


def load_reference_hit_rate_baseline(path: str | Path) -> dict[str, float]:
    """Load reference-only global hit-rate observation lines from baseline JSON.

    These values are not hard gates. Use explicit `minimum_hit_rate_by_tag` or
    CLI `--min-hit-rate` when a fixed failure threshold is intended.

    reference 命中率只用于报告观察；硬门禁使用 minimum 或 CLI 参数。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    values = raw.get("reference_hit_rate_by_tag", {})
    if not isinstance(values, dict):
        raise ValueError("cache baseline reference_hit_rate_by_tag must be an object")
    result: dict[str, float] = {}
    for key, value in values.items():
        tag = str(key).strip()
        if not tag:
            raise ValueError("reference hit-rate baseline contains empty tag")
        result[tag] = float(value)
    return result


def load_reference_warm_hit_rate_baseline(path: str | Path) -> dict[str, float]:
    """Load reference-only warm hit-rate observation lines from baseline JSON.

    These values are not hard gates. They are rendered in reports so repeated
    test runs can show whether cache reuse has converged.

    reference 字段只用于报告观察，不参与失败判定。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    values = raw.get("reference_warm_hit_rate_by_tag", {})
    if not isinstance(values, dict):
        raise ValueError("cache baseline reference_warm_hit_rate_by_tag must be an object")
    result: dict[str, float] = {}
    for key, value in values.items():
        tag = str(key).strip()
        if not tag:
            raise ValueError("reference warm cache baseline contains empty tag")
        result[tag] = float(value)
    return result


def load_reference_shape_coverage_baseline(path: str | Path) -> dict[str, float]:
    """Load reference-only prompt-shape coverage observation lines.

    These values are not hard gates. They make baseline expectations visible in
    Markdown reports without changing the CLI exit code.

    reference 字段只用于报告观察，不参与失败判定。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    values = raw.get("reference_shape_coverage_by_tag", {})
    if not isinstance(values, dict):
        raise ValueError("cache baseline reference_shape_coverage_by_tag must be an object")
    result: dict[str, float] = {}
    for key, value in values.items():
        tag = str(key).strip()
        if not tag:
            raise ValueError("reference shape coverage baseline contains empty tag")
        result[tag] = float(value)
    return result


def load_long_helper_prompt_threshold(path: str | Path) -> int:
    """Load the average prompt-token threshold for long-helper gates."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    value = raw.get("long_helper_min_prompt_tokens", 20000) if isinstance(raw, dict) else 20000
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("cache baseline long_helper_min_prompt_tokens must be an integer") from exc
    if parsed <= 0:
        raise ValueError("cache baseline long_helper_min_prompt_tokens must be positive")
    return parsed


def load_hash_chain_stability_gate_baseline(path: str | Path) -> dict[str, float]:
    """Load hash-chain stability gates from a JSON baseline file.

    Expected shape:
    {
      "maximum_unstable_hash_chain_groups_by_label": {
        "tools_loop.iter1.main": 0,
        "tools_loop.iter1.main|deepseek-v4-pro": 0
      }
    }

    基线文件声明 label 或 label|model 到允许的不稳定 hash-chain 组数上限。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    values = raw.get("maximum_unstable_hash_chain_groups_by_label", {})
    if not isinstance(values, dict):
        raise ValueError("cache baseline maximum_unstable_hash_chain_groups_by_label must be an object")
    result: dict[str, float] = {}
    for key, value in values.items():
        label = str(key).strip()
        if not label:
            raise ValueError("hash-chain baseline contains empty label")
        result[label] = float(value)
    return result


def load_prefix_hash_chain_stability_gate_baseline(path: str | Path) -> dict[str, float]:
    """Load prefix hash-chain stability gates from a JSON baseline file.

    Expected shape:
    {
      "maximum_unstable_prefix_hash_chain_groups_by_label": {
        "tools_loop.iter*.main": 0,
        "tools_loop.iter*.helper.*": 0
      }
    }

    基线文件声明核心前缀段(system_static/tool_schema)允许的不稳定组数。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    values = raw.get("maximum_unstable_prefix_hash_chain_groups_by_label", {})
    if not isinstance(values, dict):
        raise ValueError("cache baseline maximum_unstable_prefix_hash_chain_groups_by_label must be an object")
    result: dict[str, float] = {}
    for key, value in values.items():
        label = str(key).strip()
        if not label:
            raise ValueError("prefix hash-chain baseline contains empty label")
        result[label] = float(value)
    return result
