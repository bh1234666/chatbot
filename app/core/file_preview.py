"""Small, dependency-light file preview helpers for local agent clients."""
from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from app.llm.tools.output_spill import write_tool_output_spill


TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".css", ".scss", ".html", ".xml", ".csv", ".tsv",
    ".log", ".sql", ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs", ".sh",
    ".ps1", ".bat", ".cmd",
}


def preview_file(path: str | Path, *, max_chars: int = 120_000) -> dict:
    p = Path(path)
    max_chars = max(1000, min(int(max_chars or 120_000), 500_000))
    if not p.exists() or not p.is_file():
        return {"ok": False, "error": "file not found"}
    ext = p.suffix.lower()
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    base = {"ok": True, "name": p.name, "size": size, "ext": ext}

    def _text_result(kind: str, text: str) -> dict:
        truncated = len(text) > max_chars
        result = {**base, "type": kind, "content": text[:max_chars], "truncated": truncated}
        if truncated:
            saved_path = write_tool_output_spill(
                root_dir=str(p.parent),
                tool_name="file_preview",
                label=f"{kind}_content",
                text=text,
            )
            result.update({
                "content_truncated": True,
                "content_original_chars": len(text),
                "content_full_saved_path": saved_path,
                "output_truncated": True,
                "tool_result_truncated": True,
                "visible_excerpt_policy": (
                    f"Full preview text was saved at `{saved_path}` (`content_full_saved_path`); "
                    "only the head excerpt is returned."
                ),
            })
        return result

    try:
        if ext in TEXT_EXTS:
            text = p.read_text(encoding="utf-8", errors="replace")
            return _text_result("text", text)
        if ext == ".docx":
            text = _preview_docx(p)
            return _text_result("docx", text)
        if ext == ".pptx":
            text = _preview_pptx(p)
            return _text_result("pptx", text)
        if ext == ".xlsx":
            text = _preview_xlsx(p)
            return _text_result("xlsx", text)
        if ext == ".pdf":
            text = _preview_pdf(p, max_chars=max_chars)
            return _text_result("pdf", text)
    except Exception as e:
        return {**base, "ok": False, "error": f"{type(e).__name__}: {e}"}
    return {**base, "type": "binary", "content": "", "truncated": False}


def _xml_texts(raw: bytes) -> list[str]:
    root = ET.fromstring(raw)
    out: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "t" and node.text:
            out.append(node.text)
    return out


def _preview_docx(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        if "word/document.xml" in zf.namelist():
            parts.extend(_xml_texts(zf.read("word/document.xml")))
        for name in sorted(n for n in zf.namelist() if n.startswith("word/header") or n.startswith("word/footer")):
            parts.extend(_xml_texts(zf.read(name)))
    return "\n".join(x.strip() for x in parts if x.strip())


def _preview_pptx(path: Path) -> str:
    slides: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = sorted(n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        for idx, name in enumerate(names, 1):
            texts = [x.strip() for x in _xml_texts(zf.read(name)) if x.strip()]
            if texts:
                slides.append(f"# Slide {idx}\n" + "\n".join(texts))
    return "\n\n".join(slides)


def _preview_xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared = [x for x in _xml_texts(zf.read("xl/sharedStrings.xml"))]
        rows: list[list[str]] = []
        sheet_names = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        for sheet in sheet_names[:5]:
            root = ET.fromstring(zf.read(sheet))
            for row in root.iter():
                if row.tag.rsplit("}", 1)[-1] != "row":
                    continue
                values: list[str] = []
                for cell in row:
                    if cell.tag.rsplit("}", 1)[-1] != "c":
                        continue
                    cell_type = cell.attrib.get("t", "")
                    value = ""
                    for child in cell:
                        if child.tag.rsplit("}", 1)[-1] == "v" and child.text is not None:
                            value = child.text
                            break
                    if cell_type == "s" and value.isdigit():
                        idx = int(value)
                        value = shared[idx] if 0 <= idx < len(shared) else value
                    values.append(value)
                if values:
                    rows.append(values)
                if len(rows) >= 300:
                    break
            if len(rows) >= 300:
                break
    return "\n".join(",".join(_csv_escape(v) for v in row) for row in rows)


def _csv_escape(value: str) -> str:
    if any(ch in value for ch in [",", "\n", '"']):
        return json.dumps(value, ensure_ascii=False)
    return value


def _preview_pdf(path: Path, *, max_chars: int) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except Exception:
            return ""
    reader = PdfReader(str(path))
    chunks: list[str] = []
    total = 0
    for page in reader.pages[:20]:
        text = page.extract_text() or ""
        chunks.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n\n".join(chunks)
