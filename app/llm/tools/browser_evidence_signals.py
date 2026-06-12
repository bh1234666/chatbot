"""Shared browser-evidence signal detection.

Single source of truth for "does this text describe real browser-family
evidence" used by BOTH the agent runtime (helper-result summaries in
client_tools_loop) and the benchmark trace exporter. Keeping the two sides on
one module prevents definition drift — the p35088 incident had the agent
correctly running Playwright while the scorer's own markers missed it.

agent 与评测导出器共用同一套浏览器证据信号判定，防止两侧规则漂移。
"""
from __future__ import annotations

import re

# Browser automation tool/runtime names (incl. host-browser phrasing).
AUTOMATION_NAME_RE = re.compile(
    r"\b(?:playwright|puppeteer|selenium|chromium|chrome|firefox|webkit)\b"
    r"|(?:host[-\s]?browser|宿主浏览器)",
    re.IGNORECASE,
)

# Browser automation API usage markers (scripted navigation/extraction).
AUTOMATION_API_RE = re.compile(
    r"\b(?:page\.goto|goto\(|launch\(|new_page|inner_text|content\(\)|text_content|page_text|screenshot)\b"
    r"|===page_text",
    re.IGNORECASE,
)

# Evidence-style verbs that indicate something was actually observed.
EVIDENCE_VERB_RE = re.compile(
    r"\b(?:observed|saw|showed|loaded|screenshot|trace|console|passed|failed|"
    r"timeout|rendered|visited|navigated|confirmed|page\.goto|launch(?:ed)?)\b"
    r"|(?:观察|显示|加载|截图|控制台|通过|失败|超时|渲染|访问|导航|确认)",
    re.IGNORECASE,
)

# Negative boundaries: the text says browser evidence is MISSING/blocked, or
# that only plain-HTTP evidence exists. Must override positive matches.
NEGATIVE_BOUNDARY_RE = re.compile(
    r"(?:not\s+satisfied|blocked|missing|unavailable|not\s+available|"
    r"no\s+(?:playwright|puppeteer|selenium|chromium|chrome|firefox|webkit)|"
    r"not\s+(?:host[-\s]?browser|browser)\s+evidence|http\s+evidence,\s*not|"
    r"curl/plain\s+http|缺失|不可用|无法|阻塞)",
    re.IGNORECASE,
)

# HTML / HTTP-status stdout markers (page fetched through any route).
HTML_OR_STATUS_RE = re.compile(
    r"<!doctype html|<html"
    r"|\b(?:status|http)\s*[:=]?\s*20\d\b"
    r"|\[(?:ok|success)\][^\n\r]*\b20\d\b",
    re.IGNORECASE,
)


# Broad browser-topic mention (incl. bare "browser"); used for gap detection
# where any browser-boundary phrasing plus a negative marker means a gap.
BROWSER_TOPIC_RE = re.compile(
    r"\b(?:browser|playwright|puppeteer|selenium|chromium|chrome|firefox|webkit)\b"
    r"|(?:host[-\s]?browser|浏览器|宿主浏览器)",
    re.IGNORECASE,
)


def has_negative_browser_boundary(text: str) -> bool:
    """True when the text states browser evidence is missing/blocked/HTTP-only."""
    return bool(NEGATIVE_BOUNDARY_RE.search(text or ""))


def has_browser_automation_signal(command: str, output: str = "") -> bool:
    """True when a command/output pair shows scripted browser automation.

    Requires BOTH an automation tool name in the command AND an automation API
    or page-text marker somewhere — a bare mention of "chromium" in a file
    listing is not evidence.
    """
    command = command or ""
    combined = command + "\n" + (output or "")
    return bool(
        AUTOMATION_NAME_RE.search(command)
        and AUTOMATION_API_RE.search(combined)
    )


def has_browser_evidence_signal(text: str) -> bool:
    """True when free text describes browser-family evidence (tool + verb).

    Mirrors the helper-report detection: an automation/browser name plus an
    evidence verb, with negative boundaries checked by the caller via
    has_negative_browser_boundary().
    """
    text = text or ""
    return bool(AUTOMATION_NAME_RE.search(text) and EVIDENCE_VERB_RE.search(text))


def has_html_or_http_status_signal(output: str) -> bool:
    """True when stdout shows fetched HTML markup or an HTTP 2xx status."""
    return bool(HTML_OR_STATUS_RE.search(output or ""))


def has_browser_gap_signal(text: str) -> bool:
    """True when text mentions the browser boundary AND a negative marker.

    Used for extracting "browser evidence was NOT satisfied" gap facts from
    helper reports — distinct from positive evidence detection.
    """
    text = text or ""
    return bool(BROWSER_TOPIC_RE.search(text) and NEGATIVE_BOUNDARY_RE.search(text))
