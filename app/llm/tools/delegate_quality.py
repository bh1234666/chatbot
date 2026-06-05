"""Lightweight document quality helpers used by delegate outputs checks."""
from __future__ import annotations

import re


_SOURCE_DRIVEN_DOC_FORBIDDEN_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("document_internal_source_label", "内部来源/识别过程不应写入正式正文", r"OCR|视觉文字证据|识别结果"),
    ("document_unbacked_pass_standard", "PASS/FAIL 标签被扩写成了来源未给出的判定标准", r"既定标准|满足标准|通过标准|合格标准|容许范围|合格区间|未发现异常|阈值违例"),
)

_BLOCKING_QUALITY_ISSUES: set[str] = {
    # 2026-06-04 P135: blocking is reserved for unrecoverable physical failures
    # the LLM cannot resolve from the report alone. Subjective size/threshold
    # warnings (small file, few paragraphs, schema/column shape, etc.) are
    # emitted as warnings so the LLM can self-correct OR confirm the result is
    # intentional (user asked for a short/template/empty/stub artifact).
    #
    # Kept as blocking — true physical or definitionally-impossible-to-recover:
    #   - text_mojibake_suspected: encoding damage in helper-visible text
    #   - png_invalid_header / jpg_invalid_header: file is corrupt
    #   - docx_table_cell_object_literal: serialized JSON in a cell, not rendered text
    #   - requires_main_resource: helper explicitly requested a resource
    #   - stat_failed: filesystem error reading the artifact
    #
    # Demoted to warning — subjective thresholds the LLM should weigh against
    # the user's actual request:
    #   - data_file_empty / data_file_no_rows: user may want template CSV
    #   - document_too_small: user may want short/cover/template document
    #   - docx_too_few_paragraphs / docx_too_few_chars: user may want brief doc
    #   - image_too_small: user may want placeholder/icon
    #   - code_file_too_small: user may want stub/entry-point file
    #   - suspicious_short_completion: actually-delivered files exist; let LLM
    #     decide if the report adequately summarizes (0-file branch already
    #     surfaced via the "delivered_count == 0" channel separately)
    #   - benchmark_* series, csv_column_mismatch, document_internal_source_label,
    #     pptx_expected_slide_order_mismatch, document_expected_text_missing,
    #     academic_citation_unverified, docx_table_too_wide,
    #     document_unbacked_pass_standard: content / structure / shape judgments
    "docx_table_cell_object_literal",
    "png_invalid_header",
    "jpg_invalid_header",
    "requires_main_resource",
    "stat_failed",
    "text_mojibake_suspected",
}


_ACADEMIC_REFERENCE_HEADING_RE = re.compile(
    r"(?im)^\s*(references|bibliography|works cited|参考文献|参考资料)\s*$"
)
_REFERENCE_ITEM_RE = re.compile(
    r"(?im)^\s*(?:\[\d+\]|\d+[\].、)]|[-*])\s+.{8,240}$"
)
_CITATION_MARKER_RE = re.compile(
    r"\[[1-9]\d{0,2}\]|\((?:[A-Z][A-Za-z\-]+(?:\s+et\s+al\.)?,?\s+(?:19|20)\d{2})\)"
)


def academic_document_warnings(text: str) -> list[dict]:
    """Return lightweight academic-integrity warnings for generated papers.

    The check is intentionally generic. It does not judge whether a citation is
    true; it flags documents that present a reference section or citation
    markers without any visible evidence language that the references were
    supplied or verified.

    学术文档检查只做通用守卫：有引用/参考文献但缺少来源或验证说明时阻断交付。
    """
    body = str(text or "")
    if not body:
        return []
    lower = body.lower()
    looks_academic = any(
        marker in lower
        for marker in (
            "abstract",
            "methodology",
            "experiment",
            "benchmark",
            "references",
            "bibliography",
        )
    ) or any(marker in body for marker in ("摘要", "方法", "实验", "基准", "参考文献", "论文"))
    if not looks_academic:
        return []

    has_reference_section = bool(_ACADEMIC_REFERENCE_HEADING_RE.search(body))
    reference_items = _REFERENCE_ITEM_RE.findall(body)
    citation_markers = _CITATION_MARKER_RE.findall(body)
    if not (has_reference_section or reference_items or citation_markers):
        return []

    grounding_markers = (
        "verified source",
        "source evidence",
        "provided source",
        "bibliographic data",
        "doi",
        "arxiv",
        "isbn",
        "url",
        "retrieved",
        "已验证来源",
        "来源证据",
        "用户提供",
        "检索",
        "链接",
        "数据库",
    )
    if any(marker in lower or marker in body for marker in grounding_markers):
        return []

    return [{
        "issue": "academic_citation_unverified",
        # 2026-06-04 P134: severity downgraded from blocking. Citation/reference
        # presence vs grounding-evidence presence is a content judgment, not a
        # physical constraint; the LLM should self-correct on the warning. If
        # the issue persists after a resume cycle the orchestrator can still
        # escalate explicitly.
        "severity": "warning",
        "reference_items": len(reference_items),
        "citation_markers": len(citation_markers),
        "details": (
            "The academic document contains citation or reference markers but no visible source-evidence "
            "or verification basis. The helper should either verify bibliographic facts from supplied/source "
            "evidence, cite only verified local sources, or label the section as suggested further reading."
        ),
    }]


def _mojibake_score(text: str) -> int:
    """Score common Chinese mojibake patterns without flagging ordinary prose."""
    body = str(text or "")
    if not body:
        return 0
    strong_marker_chars = {
        "\u9350", "\u934f", "\u9359", "\u935b", "\u7487", "\u8292",
        "\u9207", "\u9286", "\u59dd", "\u9422", "\u6b12", "\u5815",
    }
    weak_marker_chars = {
        "\u5c7c", "\u62e2", "\u6c13", "\u6d5c", "\u6f5e", "\u7089",
        "\u732b", "\u76f2", "\u788c", "\u7984", "\u7af4", "\u7bd3",
        "\u8059", "\u805b", "\u8061",
        "\u8062", "\u8065", "\u8066", "\u8067", "\u806b", "\u806c",
        "\u8072", "\u8073", "\u8076", "\u8079", "\u807a", "\u8d42",
        "\u9732", "\u9885",
    }
    score = sum(body.count(ch) for ch in strong_marker_chars) * 3
    weak_hits = sum(body.count(ch) for ch in weak_marker_chars)
    if weak_hits >= 3:
        score += weak_hits
    score += body.count("\ufffd") * 3
    score += body.count("\u00e2\u20ac") * 2
    score += body.count("\u00c3")
    score += body.count("\u00c2")
    # Private-use characters usually mean terminal/encoding corruption in this project.
    score += sum(2 for ch in body if "\ue000" <= ch <= "\uf8ff")
    return score


def text_mojibake_warnings(text: str, *, file: str = "") -> list[dict]:
    """Return blocking warnings for likely mojibake in model-visible text.

    This is deliberately generic: it flags common UTF-8/GBK/Latin-1 corruption
    in generated reports and helper text artifacts without depending on a
    particular task or example.

    文本乱码检测用于运行产物和 helper 报告；发现明显编码错解时阻断验收。
    """
    body = str(text or "")
    if not body:
        return []
    score = _mojibake_score(body)
    if score < 3:
        return []
    excerpt = ""
    for index, ch in enumerate(body):
        if _mojibake_score(ch) > 0:
            excerpt = body[max(0, index - 80): index + 160]
            break
    return [{
        "issue": "text_mojibake_suspected",
        "severity": "blocking",
        "file": file,
        "score": score,
        "excerpt": excerpt[:260],
        "details": (
            "The text contains common mojibake markers. Regenerate or repair the text from the original "
            "UTF-8/source material before accepting this helper result."
        ),
    }]


def repair_common_mojibake_text(text: str) -> tuple[str, dict | None]:
    """Repair common UTF-8 text that was decoded through GBK/CP936 or Latin-1.

    The function returns the original text when no safe improvement is found.
    It is intended for helper-generated text files before copyback; factual
    content is not synthesized, only reversible encoding damage is repaired.

    仅修复可逆编码错解；无法安全改善时返回原文。
    """
    original = str(text or "")
    if not original:
        return original, None
    original_score = _mojibake_score(original)
    if original_score <= 0:
        return original, None
    candidates: list[tuple[str, str, int]] = []
    for encoding in ("gbk", "cp936", "latin-1"):
        try:
            repaired = original.encode(encoding, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        candidates.append((encoding, repaired, _mojibake_score(repaired)))
    if not candidates:
        return original, None
    encoding, repaired, repaired_score = min(candidates, key=lambda item: item[2])
    if repaired_score >= original_score:
        return original, None
    return repaired, {
        "encoding": encoding,
        "original_score": original_score,
        "repaired_score": repaired_score,
    }


def docx_table_structure_warnings(abs_path: str) -> list[dict]:
    """Inspect DOCX table structure for common malformed generated output."""
    warnings: list[dict] = []
    try:
        from docx import Document
        doc = Document(abs_path)
    except Exception:
        return warnings

    for table_index, table in enumerate(doc.tables):
        try:
            rows = list(table.rows)
        except Exception:
            continue
        if not rows:
            continue
        max_cols = max((len(row.cells) for row in rows), default=0)
        if max_cols >= 8:
            warnings.append({
                "issue": "docx_table_too_wide",
                # 2026-06-04 P134: warning, not blocking. 8+ columns is sometimes
                # the right choice (benchmark comparison tables); let the LLM decide.
                "severity": "warning",
                "table_index": table_index,
                "columns": max_cols,
                "details": (
                    "The DOCX contains a wide table. For Word papers/reports, consider whether splitting the "
                    "comparison into smaller tables or prose sections would improve readability."
                ),
            })
        object_literal_cells: list[dict] = []
        long_cells = 0
        for row_index, row in enumerate(rows):
            for col_index, cell in enumerate(row.cells):
                cell_text = (cell.text or "").strip()
                compact = re.sub(r"\s+", "", cell_text)
                if re.search(r"^\{['\"]?(?:text|value|content|style)['\"]?\s*:", compact):
                    object_literal_cells.append({
                        "row": row_index,
                        "col": col_index,
                        "excerpt": cell_text[:120],
                    })
                if len(cell_text) >= 260:
                    long_cells += 1
        if object_literal_cells:
            warnings.append({
                "issue": "docx_table_cell_object_literal",
                "severity": "blocking",
                "table_index": table_index,
                "cells": object_literal_cells[:8],
                "details": (
                    "A DOCX table cell contains a serialized object instead of rendered text. "
                    "Rewrite the table with plain cell text or supported text/style objects."
                ),
            })
        if rows and long_cells >= max(3, len(rows)):
            warnings.append({
                "issue": "docx_table_too_wide",
                # 2026-06-04 P134: warning, not blocking. Long-cell prose vs
                # smaller tables is a layout judgment; LLM can self-correct.
                "severity": "warning",
                "table_index": table_index,
                "long_cell_count": long_cells,
                "details": (
                    "Many table cells contain paragraph-length prose. Consider converting this section to "
                    "prose, bullets, or smaller focused tables for readability."
                ),
            })
    return warnings


def document_source_grounding_warnings(text: str) -> list[dict]:
    """Warn when a source-driven document leaks internal source labels or overclaims."""
    body = str(text or "")
    if not body:
        return []
    warnings: list[dict] = []
    for issue, details, pattern in _SOURCE_DRIVEN_DOC_FORBIDDEN_PATTERNS:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 40)
            end = min(len(body), m.end() + 40)
            warnings.append({
                "issue": issue,
                "details": details,
                "match": m.group(0),
                "excerpt": body[start:end],
            })
    return warnings


def blocking_quality_warnings(warnings: list[dict] | None) -> list[dict]:
    """Return warnings that should block accepting a helper as complete."""
    out: list[dict] = []
    for warning in warnings or []:
        if not isinstance(warning, dict):
            continue
        severity = str(warning.get("severity") or "").strip().lower()
        issue = str(warning.get("issue") or "").strip()
        if severity == "blocking" or issue in _BLOCKING_QUALITY_ISSUES:
            out.append(warning)
    return out


def _extract_expected_text_tokens_for_document(prompt: str) -> list[str]:
    """Pull concrete user-requested text snippets for lightweight document QA."""
    text = str(prompt or "")
    tokens: list[str] = []
    for m in re.finditer(r"[“\"']([^“”\"'\n]{2,40})[”\"']", text):
        val = m.group(1).strip()
        sentence_start = max(text.rfind("。", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        prefix = text[sentence_start:m.start()]
        suffix = text[m.end():m.end() + 8]
        if re.search(r"(不要|不得|不能|禁止|避免|不应|不要出现|不得出现|不能出现|全文不得出现)", prefix + suffix):
            continue
        if val and not re.fullmatch(r"[\d\s,，.。:：;；、=-]+", val):
            tokens.append(val)
    for m in re.finditer(
        r"\b([A-Za-z][A-Za-z0-9_ -]{0,20})\s*[=＝:：]\s*(-?\d+(?:\.\d+)?)\b",
        text,
    ):
        tokens.append(f"{m.group(1).strip()}={m.group(2).strip()}")
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        norm = re.sub(r"\s+", "", token)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(token)
    return out[:20]


def _normalize_doc_text_for_match(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).replace("＝", "=").replace("：", ":")


def _document_text_for_quality_check(abs_path: str, ext: str) -> str:
    ext = ext.lower().lstrip(".")
    try:
        if ext == "pptx":
            from app.llm.tools.file_inspect import _inspect_pptx
            meta = _inspect_pptx(abs_path)
            if isinstance(meta, dict):
                return "\n".join(str(x) for x in (meta.get("slide_texts") or []))
        if ext == "docx":
            import html as _html
            import zipfile as _zipfile
            with _zipfile.ZipFile(abs_path) as zf:
                xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
            return "\n".join(
                _html.unescape(m.group(1))
                for m in re.finditer(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.S)
            )
        if ext == "xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(abs_path, read_only=True, data_only=True)
            parts: list[str] = []
            try:
                for ws in wb.worksheets[:5]:
                    for row in ws.iter_rows(max_row=30, max_col=20, values_only=True):
                        for val in row:
                            if val is not None:
                                parts.append(str(val))
            finally:
                wb.close()
            return "\n".join(parts)
    except Exception:
        return ""
    return ""


def _pptx_slide_texts_for_quality_check(abs_path: str) -> list[str]:
    try:
        from app.llm.tools.file_inspect import _inspect_pptx
        meta = _inspect_pptx(abs_path)
        if isinstance(meta, dict):
            return [str(x) for x in (meta.get("slide_texts") or [])]
    except Exception:
        return []
    return []


def _zh_num_to_int(text: str) -> int | None:
    text = str(text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    vals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if text in vals:
        return vals[text]
    if text.startswith("十") and len(text) == 2 and text[1] in vals:
        return 10 + vals[text[1]]
    if text.endswith("十") and len(text) == 2 and text[0] in vals:
        return vals[text[0]] * 10
    if "十" in text and len(text) == 3 and text[0] in vals and text[2] in vals:
        return vals[text[0]] * 10 + vals[text[2]]
    return None


def _extract_expected_ppt_slide_token_groups(prompt: str) -> dict[int, list[str]]:
    """Extract per-slide expected concrete tokens from prompts like '第 2 页...X=12'."""
    text = str(prompt or "")
    matches = list(re.finditer(r"第\s*([一二两三四五六七八九十\d]{1,3})\s*(?:页|张|个?幻灯片)", text))
    if len(matches) < 2:
        return {}
    groups: dict[int, list[str]] = {}
    for idx, match in enumerate(matches):
        slide_no = _zh_num_to_int(match.group(1))
        if not slide_no:
            continue
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        segment = text[match.end():end]
        tokens: list[str] = []
        for q in re.finditer(r"[“\"']([^“”\"'\n]{2,40})[”\"']", segment):
            tokens.append(q.group(1).strip())
        for kv in re.finditer(r"\b([A-Za-z][A-Za-z0-9_ -]{0,20})\s*[=＝:：]\s*(-?\d+(?:\.\d+)?)\b", segment):
            tokens.append(f"{kv.group(1).strip()}={kv.group(2).strip()}")
        seen: set[str] = set()
        clean: list[str] = []
        for token in tokens:
            norm = _normalize_doc_text_for_match(token)
            if norm and norm not in seen:
                seen.add(norm)
                clean.append(token)
        if clean:
            groups[slide_no] = clean[:10]
    return groups
