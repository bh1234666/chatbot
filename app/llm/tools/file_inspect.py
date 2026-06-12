"""文件类型探查:docx/pptx/xlsx/image/pdf/wav 元信息探查 + 警告探查。

2026-05-20 重构: 从 llm/tools/workspace.py 原样抽出。closure 自包含(7 函数, 0 unsafe),
仅依赖 stdlib(os/re);第三方解析库在函数内惰性 import。workspace.py re-export 兼容。
"""
import html
import os
import re


_DOCX_STALE_CHART_PLACEHOLDER_PHRASES = (
    "PNG 尚未生成",
    "图表 PNG 尚未生成",
    "图片尚未生成",
    "图表尚未生成",
    "本文不嵌入图片",
    "不嵌入图片",
    "不嵌图",
    "性能图表预留",
    "图表预留章节",
    "预留图表章节",
    "后续补充图表",
    "后续再补充图表",
    "后续可补充图表",
)


def _inspect_docx(abs_path: str) -> dict:
    """docx = zip + word/document.xml + word/media/*。粗略 grep 计数。"""
    import zipfile
    try:
        with zipfile.ZipFile(abs_path) as zf:
            names = zf.namelist()
            images = [n for n in names if n.startswith("word/media/")]
            try:
                doc_xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
            except KeyError:
                return {"error": "DOCX is missing word/document.xml and may be damaged.\nDOCX 缺少主体 XML，可能损坏。"}
            # 粗略计数(<w:p ...> 段落,<w:tbl> 表格)
            para_count = doc_xml.count("<w:p ") + doc_xml.count("<w:p>")
            table_count = doc_xml.count("<w:tbl>")
            # 文本字数(去掉 XML 标签)
            text_only = re.sub(r"<[^>]+>", "", doc_xml)
            char_count = len(text_only.strip())
            # 提取一段文字预览(第一个 <w:t>...</w:t>)
            first_texts = re.findall(r"<w:t[^>]*>([^<]+)</w:t>", doc_xml)
            full_text = "".join(html.unescape(t) for t in first_texts)
            preview = full_text[:200]
            stale_chart_hit = next(
                (phrase for phrase in _DOCX_STALE_CHART_PLACEHOLDER_PHRASES if phrase in full_text),
                "",
            )
            return {
                "paragraph_count": para_count,
                "table_count": table_count,
                "image_count": len(images),
                "image_files": [os.path.basename(p) for p in images[:10]],
                "text_chars": char_count,
                "text_preview": preview,
                "stale_chart_placeholder_hit": stale_chart_hit,
            }
    except (zipfile.BadZipFile, OSError) as e:
        return {"error": f"DOCX inspection failed; the file may be damaged or not a real DOCX: {e}.\nDOCX 解析失败，可能损坏或格式不真实。"}


def _inspect_pptx(abs_path: str) -> dict:
    """pptx = zip + ppt/slides/slideN.xml + ppt/media/*。"""
    import zipfile
    try:
        with zipfile.ZipFile(abs_path) as zf:
            names = zf.namelist()
            slides = sorted(
                n for n in names
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
            media = [n for n in names if n.startswith("ppt/media/")]
            # 提取每张 slide 的可见文字。PPT 表格单元格也在 <a:t> 中；
            # 只返回标题会漏掉关键数据页内容。
            slide_titles = []
            slide_texts = []
            for s in slides[:20]:
                try:
                    xml = zf.read(s).decode("utf-8", errors="replace")
                    texts = [
                        html.unescape(m.group(1)).strip()
                        for m in re.finditer(r"<a:t[^>]*>(.*?)</a:t>", xml, flags=re.S)
                    ]
                    texts = [t for t in texts if t]
                    title = texts[0][:80] if texts else "(untitled)"
                    slide_titles.append(title)
                    slide_texts.append(" ".join(texts)[:1000])
                except Exception:
                    slide_titles.append("(parse failed)")
                    slide_texts.append("")
            text_preview = "\n".join(t for t in slide_texts if t)[:2000]
            return {
                "slide_count": len(slides),
                "image_count": len(media),
                "image_files": [os.path.basename(p) for p in media[:10]],
                "slide_titles": slide_titles,
                "slide_texts": slide_texts,
                "text_chars": sum(len(t) for t in slide_texts),
                "text_preview": text_preview,
            }
    except (zipfile.BadZipFile, OSError) as e:
        return {"error": f"PPTX inspection failed: {e}.\nPPTX 解析失败。"}


def _inspect_xlsx(abs_path: str) -> dict:
    """xlsx = zip + xl/worksheets/sheetN.xml。"""
    import zipfile
    try:
        with zipfile.ZipFile(abs_path) as zf:
            names = zf.namelist()
            sheet_files = sorted(
                n for n in names
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            )
            # 读 xl/workbook.xml 拿 sheet name
            sheet_names = []
            try:
                wb_xml = zf.read("xl/workbook.xml").decode("utf-8", errors="replace")
                sheet_names = re.findall(r'<sheet [^/]*name="([^"]+)"', wb_xml)
            except KeyError:
                pass
            # 每个 sheet 的行数
            rows_per_sheet = []
            for sf in sheet_files[:10]:
                try:
                    content = zf.read(sf).decode("utf-8", errors="replace")
                    row_count = content.count("<row ")
                    rows_per_sheet.append({
                        "sheet_file": os.path.basename(sf),
                        "row_count": row_count,
                    })
                except Exception:
                    pass
            return {
                "sheet_count": len(sheet_files),
                "sheet_names": sheet_names,
                "rows_per_sheet": rows_per_sheet,
            }
    except (zipfile.BadZipFile, OSError) as e:
        return {"error": f"XLSX inspection failed: {e}.\nXLSX 解析失败。"}


def _inspect_image(abs_path: str, ext: str) -> dict:
    """PNG / JPEG / GIF / BMP / WebP 头解析(无需 PIL)。"""
    import struct
    try:
        with open(abs_path, "rb") as f:
            head = f.read(64)
        if not head:
            return {"error": "The file is empty.\n文件为空。"}
        if ext == ".png":
            if head[:8] != b"\x89PNG\r\n\x1a\n":
                return {"error": "Invalid PNG signature.\nPNG 签名无效。"}
            width = struct.unpack(">I", head[16:20])[0]
            height = struct.unpack(">I", head[20:24])[0]
            return {"format": "PNG", "width": width, "height": height}
        elif ext in (".jpg", ".jpeg"):
            # 扫描 SOFn marker (0xFFC0–0xFFCF, 跳过 0xC4/C8/CC)
            with open(abs_path, "rb") as f:
                data = f.read(65536)
            if data[:2] != b"\xff\xd8":
                return {"error": "Missing JPEG SOI marker.\nJPEG SOI 标记缺失。"}
            i = 2
            while i < len(data) - 8:
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height = struct.unpack(">H", data[i + 5:i + 7])[0]
                    width = struct.unpack(">H", data[i + 7:i + 9])[0]
                    return {"format": "JPEG", "width": width, "height": height}
                if i + 4 > len(data):
                    break
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg_len
            return {"format": "JPEG", "note": "No SOFn marker was found in the first 64KB.\n首 64KB 内未找到 SOFn。"}
        elif ext == ".gif":
            if head[:6] not in (b"GIF87a", b"GIF89a"):
                return {"error": "Invalid GIF signature.\nGIF 签名无效。"}
            width = struct.unpack("<H", head[6:8])[0]
            height = struct.unpack("<H", head[8:10])[0]
            return {"format": "GIF", "width": width, "height": height}
        elif ext == ".bmp":
            if head[:2] != b"BM":
                return {"error": "Invalid BMP signature.\nBMP 签名无效。"}
            width = struct.unpack("<I", head[18:22])[0]
            height = struct.unpack("<I", head[22:26])[0]
            return {"format": "BMP", "width": width, "height": height}
        elif ext == ".webp":
            if head[:4] != b"RIFF" or head[8:12] != b"WEBP":
                return {"error": "Invalid WebP signature.\nWebP 签名无效。"}
            return {"format": "WebP", "note": "Image dimensions need deeper parsing and are not implemented here.\n尺寸需要更深入解析。"}
        return {"format": ext.lstrip(".").upper(), "note": "This image format is not implemented for metadata inspection.\n该图片格式暂未实现元数据解析。"}
    except OSError as e:
        return {"error": f"image inspection failed: {e}"}


def _inspect_pdf(abs_path: str) -> dict:
    """PDF 粗略页数:统计 /Type /Page (排除 /Pages)。"""
    try:
        with open(abs_path, "rb") as f:
            data = f.read()
        if not data.startswith(b"%PDF"):
            return {"error": "Invalid PDF signature.\nPDF 签名无效。"}
        version = data[5:8].decode("ascii", errors="replace")
        page_objs = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
        # 后备:数 /Page (无空格分隔的 obj 头)
        if page_objs <= 0:
            page_objs = data.count(b"/Type/Page") - data.count(b"/Type/Pages")
        return {
            "format": "PDF",
            "version": version,
            "page_count": max(0, page_objs),
        }
    except OSError as e:
        return {"error": f"PDF inspection failed: {e}"}


def _inspect_wav(abs_path: str) -> dict:
    """WAV header(RIFF/fmt)。"""
    import struct
    try:
        with open(abs_path, "rb") as f:
            head = f.read(44)
        if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return {"error": "Invalid WAV signature.\nWAV 签名无效。"}
        if head[12:16] != b"fmt ":
            return {"error": "The fmt chunk is in an atypical position; this may be a non-standard WAV.\nfmt 块位置非典型。"}
        channels = struct.unpack("<H", head[22:24])[0]
        sample_rate = struct.unpack("<I", head[24:28])[0]
        byte_rate = struct.unpack("<I", head[28:32])[0]
        bits_per_sample = struct.unpack("<H", head[34:36])[0]
        size = os.path.getsize(abs_path)
        duration = (size - 44) / byte_rate if byte_rate else 0
        return {
            "format": "WAV",
            "channels": channels,
            "sample_rate": sample_rate,
            "bits_per_sample": bits_per_sample,
            "duration_seconds": round(duration, 2),
        }
    except OSError as e:
        return {"error": f"WAV inspection failed: {e}"}


def _inspect_wav(abs_path: str) -> dict:
    """Inspect RIFF/WAVE files by scanning chunks instead of assuming a 44-byte PCM header."""
    import struct
    try:
        with open(abs_path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return {"error": "invalid WAV signature"}

            fmt = None
            data_size = None
            while True:
                chunk_head = f.read(8)
                if len(chunk_head) < 8:
                    break
                chunk_id = chunk_head[:4]
                chunk_size = struct.unpack("<I", chunk_head[4:8])[0]
                chunk_data = f.read(chunk_size)
                if chunk_id == b"fmt " and len(chunk_data) >= 16:
                    fmt = chunk_data
                elif chunk_id == b"data":
                    data_size = chunk_size
                if chunk_size % 2:
                    f.seek(1, os.SEEK_CUR)
                if fmt is not None and data_size is not None:
                    break

        if fmt is None:
            return {"error": "missing fmt chunk"}

        audio_format = struct.unpack("<H", fmt[0:2])[0]
        channels = struct.unpack("<H", fmt[2:4])[0]
        sample_rate = struct.unpack("<I", fmt[4:8])[0]
        byte_rate = struct.unpack("<I", fmt[8:12])[0]
        bits_per_sample = struct.unpack("<H", fmt[14:16])[0]
        size = os.path.getsize(abs_path)
        payload_size = data_size if data_size is not None else max(0, size - 44)
        duration = payload_size / byte_rate if byte_rate else 0
        format_name = {
            1: "PCM",
            3: "IEEE_FLOAT",
            65534: "EXTENSIBLE",
        }.get(audio_format, f"UNKNOWN_{audio_format}")
        return {
            "format": "WAV",
            "audio_format": audio_format,
            "audio_format_name": format_name,
            "channels": channels,
            "sample_rate": sample_rate,
            "bits_per_sample": bits_per_sample,
            "duration_seconds": round(duration, 2),
        }
    except OSError as e:
        return {"error": f"WAV inspection failed: {e}"}


def _inspect_warnings(result: dict) -> list[str]:
    """Generate metadata warnings for the model; delivery decisions stay with the LLM."""
    warnings = []
    md = result.get("metadata") or {}
    file_type = result.get("type", "")
    size = result.get("size_bytes", 0)

    if size == 0:
        warnings.append("File size is 0 bytes. This is factual evidence that the artifact has no stored content.")
    elif size < 100 and file_type in ("docx", "pptx", "xlsx", "pdf"):
        warnings.append(f"⚠️ {file_type} 文件极小(<100 字节),可能损坏")

    if file_type == "docx":
        para = md.get("paragraph_count", 0)
        tbl = md.get("table_count", 0)
        img = md.get("image_count", 0)
        chars = md.get("text_chars", 0)
        if para == 0 and tbl == 0 and chars == 0:
            warnings.append("⚠️ docx 完全空白(0 段落 0 表格 0 字)")
        elif chars < 50:
            warnings.append(f"⚠️ docx 正文极短({chars} 字),可能未填充")
        # 2026-05-09 Patch 32a: 大量段落但 0 张图 — 论文/报告类常态是包含图表,
        # 全文本输出大概率说明 helper 只更新了文字、忘记跑图表脚本/嵌入图。
        # trace 779bbcf0 实测:helper update_paper 出 219 段 8455 字的 docx 但 image_count=0,
        # 主线程 inspect 两次看到 0 仍交付 → 用户拿到无图论文。
        # 阈值 80 段:短备忘录 / 会议纪要 < 80 段不会触发,论文/长报告 ≥ 100 段必触发。
        if para >= 80 and img == 0:
            warnings.append(
                f"docx has {para} paragraphs and 0 embedded images. For papers or long reports, compare this fact "
                f"with the user's requested deliverable and any stated chart/figure requirements before deciding whether "
                f"the document is complete."
            )
        if img > 0:
            hit = md.get("stale_chart_placeholder_hit") or ""
            if hit:
                warnings.append(
                    "docx contains embedded images and also contains transitional chart-placeholder wording "
                    f"({hit}). Read the relevant text before deciding whether the figure status is consistent."
                )
    elif file_type == "pptx":
        slides = md.get("slide_count", 0)
        img = md.get("image_count", 0)
        if slides == 0:
            warnings.append("⚠️ pptx 0 幻灯片")
        # 2026-05-09 Patch 32a: pptx 5+ 页但 0 张图 — 演示文稿没图基本不达标
        elif slides >= 5 and img == 0:
            warnings.append(
                f"pptx has {slides} slides and 0 embedded images. Compare this fact with the requested presentation requirements."
            )
    elif file_type == "xlsx":
        rps = md.get("rows_per_sheet", [])
        if all(s.get("row_count", 0) <= 1 for s in rps):
            warnings.append("⚠️ xlsx 所有 sheet 均空或仅含表头")
    elif file_type == "image":
        w = md.get("width", 0)
        h = md.get("height", 0)
        if w * h == 0:
            warnings.append("⚠️ 图片尺寸 0×0 或解析失败")
        elif w < 50 or h < 50:
            warnings.append(f"⚠️ 图片极小({w}×{h}),可能是占位/出错图")

    return warnings


_inspect_warnings_base = _inspect_warnings


def _inspect_warnings(result: dict) -> list[str]:
    warnings = _inspect_warnings_base(result)
    md = result.get("metadata") or {}
    if result.get("type") == "wav":
        if md.get("audio_format") not in (1, 65534):
            warnings.append("WAV is not PCM encoded; some tools may fail to read it")
        if md.get("bits_per_sample") not in (16, 24, 32):
            warnings.append(f"WAV has unusual bit depth: {md.get('bits_per_sample')}")
    return warnings
