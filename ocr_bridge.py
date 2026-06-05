# -*- coding: utf-8 -*-
"""
OCR bridge — 优先走 MinerU，失败时自动回退到 Umi-OCR。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Union

# MinerU 在检测到图像区域但提取不到文字时，会在输出 .md 中写入占位图片引用，形如
#   ![](images/<hash>.jpg)
# 这不是 OCR 文本——若把它当成 text 返回给 LLM，score=1.0 会让模型误以为识别成功，
# 实际却只拿到一个无意义的文件名串。此正则用于识别并剥离这类占位符。
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _strip_markdown_images(text: str) -> str:
    """Remove `![alt](path)` markdown image references."""
    return _MD_IMAGE_RE.sub("", text)


def _has_text_content(text: str) -> bool:
    """True iff text has real characters, not just markdown image refs / whitespace."""
    stripped = _strip_markdown_images(text).strip()
    return bool(stripped)

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_UMI_DIR = _PROJECT_ROOT / "umi-ocr" / "Umi-OCR_Paddle_v2.1.5"
_OCR_SCRIPT = _PROJECT_ROOT / "umi-ocr" / "ocr_headless.py"
_MINERU_DIR = _PROJECT_ROOT / "mineru"
_MINERU_CLIENT = _MINERU_DIR / "py310" / "Lib" / "site-packages" / "mineru" / "cli" / "client.py"
_MINERU_HF_DIR = _MINERU_DIR / "hf"
_MINERU_PIPELINE_REPO = "models--opendatalab--PDF-Extract-Kit-1.0"
_MINERU_VLM_REPO = "models--opendatalab--MinerU2.5-Pro-2604-1.2B"

_DEFAULT_TIMEOUT = 120
_ARGV_BASE64_SAFE_LIMIT = 16 * 1024
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".tiff"}
_OFFICE_SUFFIXES = {".docx", ".pptx", ".xlsx"}
_MINERU_SUFFIXES = _IMAGE_SUFFIXES | _OFFICE_SUFFIXES | {".pdf"}


@dataclass
class OcrResult:
    ok: bool
    text: str = ""
    score: float = 0.0
    error: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _resolve_umi_runtime() -> Path:
    env_override = os.environ.get("UMI_OCR_RUNTIME", "").strip()
    if env_override:
        return Path(env_override)
    if sys.platform.startswith("win"):
        return _UMI_DIR / "UmiOCR-data" / "runtime" / "python.exe"
    candidates = [
        _UMI_DIR / "UmiOCR-data" / "runtime" / "python3",
        _UMI_DIR / "UmiOCR-data" / "runtime" / "bin" / "python3",
        Path("/usr/bin/python3"),
        Path("/usr/local/bin/python3"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return _UMI_DIR / "UmiOCR-data" / "runtime" / "python3"


def _resolve_mineru_runtime() -> Path:
    env_override = os.environ.get("MINERU_PYTHON", "").strip()
    if env_override:
        return Path(env_override)
    if sys.platform.startswith("win"):
        return _MINERU_DIR / "py310" / "python.exe"
    return _MINERU_DIR / "py310" / "bin" / "python"


_UMI_RUNTIME = _resolve_umi_runtime()
_MINERU_RUNTIME = _resolve_mineru_runtime()


def _subprocess_creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _latest_snapshot(repo_dir: Path) -> Path | None:
    snapshots = repo_dir / "snapshots"
    if not snapshots.is_dir():
        return None
    candidates = [p for p in snapshots.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _mineru_local_model_config() -> Path | None:
    pipeline = _latest_snapshot(_MINERU_HF_DIR / _MINERU_PIPELINE_REPO)
    vlm = _latest_snapshot(_MINERU_HF_DIR / _MINERU_VLM_REPO)
    if pipeline is None or vlm is None:
        return None

    cfg = _MINERU_DIR / "mineru.local.json"
    data = {
        "config_version": "1.3.1",
        "models-dir": {
            "pipeline": str(pipeline),
            "vlm": str(vlm),
        },
    }
    try:
        existing = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else None
    except Exception:
        existing = None
    if existing != data:
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _mineru_env() -> dict[str, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cfg = _mineru_local_model_config()
    if cfg is not None:
        env.setdefault("MINERU_MODEL_SOURCE", "local")
        env.setdefault("MINERU_TOOLS_CONFIG_JSON", str(cfg))
        env.setdefault("HF_HOME", str(_MINERU_HF_DIR))
        env.setdefault("HUGGINGFACE_HUB_CACHE", str(_MINERU_HF_DIR))
    # 2026-05-18 P167: MinerU set_lmdeploy_backend() on Windows reads
    # os.getenv("lmdeploy_backend") (lowercase, no MINERU_ prefix) due to an
    # upstream bug. Without this, hybrid/VLM backends crash with
    # "Unsupported lmdeploy backend: None". Setting the lowercase var as a
    # workaround so hybrid-auto-engine can resolve to pytorch backend.
    if sys.platform == "win32":
        env.setdefault("lmdeploy_backend", "pytorch")
        env.setdefault("MINERU_LMDEPLOY_DEVICE", "cuda")
        env.setdefault("MINERU_LMDEPLOY_BACKEND", "pytorch")
    return env


def _legacy_available() -> bool:
    return _UMI_RUNTIME.is_file() and _OCR_SCRIPT.is_file()


def _mineru_available() -> bool:
    return _MINERU_RUNTIME.is_file() and _MINERU_CLIENT.is_file()


# ── 2026-05-18 P167: persistent background service discovery ────────────
_PORT_FILE = _MINERU_DIR / ".mineru_api_port"


def _mineru_bg_api_url() -> str | None:
    """Return the API URL of a persistent mineru-api background service, if alive."""
    if not _PORT_FILE.is_file():
        return None
    try:
        port = int(_PORT_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=2)
        return f"http://127.0.0.1:{port}"
    except Exception:
        return None


def _call_legacy_headless(args: list[str], timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if not _legacy_available():
        return OcrResult(ok=False, error=f"Umi-OCR runtime not found: {_UMI_RUNTIME}")

    cmd = [str(_UMI_RUNTIME), str(_OCR_SCRIPT)] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=_subprocess_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return OcrResult(ok=False, error=f"OCR timeout after {timeout}s")
    except FileNotFoundError:
        return OcrResult(ok=False, error=f"Runtime missing: {_UMI_RUNTIME}")
    except OSError as e:
        return OcrResult(ok=False, error=f"subprocess OSError: {e}")
    except Exception as e:
        return OcrResult(ok=False, error=f"subprocess error: {e}")

    stdout = proc.stdout.strip() if proc.stdout else ""
    stderr = proc.stderr.strip() if proc.stderr else ""

    if stderr and "PPOCR" not in stderr:
        log.warning("OCR stderr: %s", stderr[:200])

    if not stdout:
        stderr_clean = "\n".join(ln for ln in stderr.splitlines() if "PPOCR" not in ln).strip()
        err_msg = f"OCR produced no output (rc={proc.returncode})"
        if stderr_clean:
            err_msg += f"; stderr tail: {stderr_clean[-300:]}"
        return OcrResult(ok=False, error=err_msg)

    json_line = stdout.split("\n", 1)[0].strip()
    try:
        data = json.loads(json_line)
    except json.JSONDecodeError as e:
        return OcrResult(ok=False, error=f"JSON parse error: {e} (raw: {json_line[:200]})")

    if data.get("ok"):
        return OcrResult(ok=True, text=data.get("text", ""), score=data.get("score", 0.0))
    return OcrResult(ok=False, error=data.get("error", "unknown OCR error"))


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _mineru_error_excerpt(proc: subprocess.CompletedProcess[str]) -> str:
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    parts: list[str] = []
    if stderr:
        parts.append(stderr[-500:])
    if stdout:
        parts.append(stdout[-500:])
    return " | ".join(p for p in parts if p)


def _collect_mineru_text(output_dir: Path, stem: str) -> str:
    candidates = sorted(output_dir.rglob(f"{stem}.md"))
    if not candidates:
        candidates = sorted(output_dir.rglob("*.md"))
    for candidate in candidates:
        try:
            text = _normalize_text(candidate.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if text:
            return text
    return ""


def _ocr_with_mineru_file(path: Path, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if not _mineru_available():
        return OcrResult(ok=False, error=f"MinerU runtime not found: {_MINERU_RUNTIME}")

    with tempfile.TemporaryDirectory(prefix="mineru_ocr_") as td:
        output_dir = Path(td) / "out"
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(_MINERU_RUNTIME),
            "-m",
            "mineru.cli.client",
            "-p",
            str(path),
            "-o",
            str(output_dir),
            "-b",
            "pipeline",
            "-m",
            "auto",
            "-l",
            "ch",
            "-f",
            "true",
            "-t",
            "true",
        ]
        # 2026-05-18 P167: reuse persistent background service when available
        # to skip ~30 s cold-start model loading per OCR call.
        bg_url = _mineru_bg_api_url()
        if bg_url:
            cmd.extend(["--api-url", bg_url])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                env=_mineru_env(),
                creationflags=_subprocess_creationflags(),
            )
        except subprocess.TimeoutExpired:
            return OcrResult(ok=False, error=f"MinerU timeout after {timeout}s")
        except FileNotFoundError:
            return OcrResult(ok=False, error=f"MinerU runtime missing: {_MINERU_RUNTIME}")
        except OSError as e:
            return OcrResult(ok=False, error=f"MinerU subprocess OSError: {e}")
        except Exception as e:
            return OcrResult(ok=False, error=f"MinerU subprocess error: {e}")

        text = _collect_mineru_text(output_dir, path.stem)
        # 2026-05-18 P167: MinerU is the primary OCR engine.  Only treat
        # hard crashes (subprocess error, timeout, non-zero rc) as failures.
        # Soft issues (image placeholders, partial/garbled text) stay as ok=True
        # — downstream P164 math_quality_warning handles content warnings.
        # Umi-OCR fallback is ONLY for "MinerU couldn't run at all."
        if proc.returncode == 0:
            return OcrResult(ok=True, text=text, score=1.0)

        excerpt = _mineru_error_excerpt(proc)
        if excerpt:
            return OcrResult(ok=False, error=f"MinerU failed (rc={proc.returncode}): {excerpt}")
        return OcrResult(ok=False, error=f"MinerU failed (rc={proc.returncode})")


def _render_pdf_to_images(pdf_path: Path) -> tuple[list[Path], callable]:
    import fitz

    temp_dir = Path(tempfile.mkdtemp(prefix="ocr_pdf_"))
    created: list[Path] = []
    doc = fitz.open(pdf_path)
    try:
        for idx in range(len(doc)):
            page = doc.load_page(idx)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            out = temp_dir / f"page_{idx + 1}.png"
            pix.save(str(out))
            created.append(out)
    finally:
        doc.close()

    def _cleanup() -> None:
        for item in created:
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            temp_dir.rmdir()
        except OSError:
            pass

    return created, _cleanup


def _ocr_pdf_with_legacy(pdf_path: Path, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if not _legacy_available():
        return OcrResult(ok=False, error="legacy OCR unavailable for PDF fallback")
    try:
        image_paths, cleanup = _render_pdf_to_images(pdf_path)
    except Exception as e:
        return OcrResult(ok=False, error=f"PDF render failed: {e}")

    try:
        texts: list[str] = []
        for image_path in image_paths:
            result = _ocr_with_legacy_file(image_path, timeout=timeout)
            if not result.ok:
                return result
            text = _normalize_text(result.text)
            if text:
                texts.append(text)
        combined = "\n\n".join(texts).strip()
        if combined:
            return OcrResult(ok=True, text=combined, score=1.0)
        return OcrResult(ok=False, error="PDF OCR produced empty text")
    finally:
        cleanup()


def _image_suffix_from_data(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return ".pdf"
    try:
        from PIL import Image
        from io import BytesIO

        with Image.open(BytesIO(data)) as img:
            fmt = (img.format or "").lower()
    except Exception:
        return ".img"
    if fmt == "jpeg":
        return ".jpg"
    if fmt:
        return f".{fmt}"
    return ".img"


def _ensure_supported_image_path(path: Path) -> tuple[Path, callable | None]:
    ext = path.suffix.lower()
    if ext in _MINERU_SUFFIXES:
        return path, None
    try:
        from PIL import Image
    except Exception:
        return path, None

    temp_path: Path | None = None
    try:
        with Image.open(path) as img:
            if getattr(img, "is_animated", False):
                img.seek(0)
            converted = img.convert("RGB")
            fd, raw = tempfile.mkstemp(prefix="ocr_adapt_", suffix=".png")
            os.close(fd)
            temp_path = Path(raw)
            converted.save(temp_path, format="PNG")
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return path, None

    def _cleanup() -> None:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    return temp_path or path, _cleanup


def _is_legacy_supported_file(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES or path.suffix.lower() == ".pdf"


def _ocr_with_legacy_file(path: Union[str, Path], *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    target = Path(path)
    if not target.is_file():
        return OcrResult(ok=False, error=f"image file not found: {target}")
    if not _is_legacy_supported_file(target):
        return OcrResult(ok=False, error=f"legacy OCR does not support {target.suffix.lower() or 'suffixless'} files")
    if target.suffix.lower() == ".pdf":
        return _ocr_pdf_with_legacy(target, timeout=timeout)
    prepared, cleanup = _ensure_supported_image_path(target)
    try:
        return _call_legacy_headless([str(prepared)], timeout=timeout)
    finally:
        if cleanup is not None:
            cleanup()


def ocr_file(path: Union[str, Path], *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    target = Path(path)
    if not target.is_file():
        return OcrResult(ok=False, error=f"image file not found: {target}")

    prepared, cleanup = _ensure_supported_image_path(target)
    try:
        if prepared.suffix.lower() in _MINERU_SUFFIXES:
            mineru_result = _ocr_with_mineru_file(prepared, timeout=timeout)
            # 2026-05-18 P167: MinerU is the primary engine.  Only fall back
            # to Umi-OCR on HARD CRASH (subprocess error, timeout, GPU OOM).
            # Soft degradation (partial text, image placeholders) stays MinerU.
            if mineru_result.ok:
                return mineru_result
            # Hard crash — try legacy as last resort
            legacy_result = _ocr_with_legacy_file(prepared, timeout=timeout)
            if legacy_result.ok:
                return legacy_result
            return OcrResult(
                ok=False,
                error=f"MinerU: {mineru_result.error}; fallback: {legacy_result.error}",
            )

        # Suffix not supported by MinerU — legacy only
        return _ocr_with_legacy_file(prepared, timeout=timeout)
    finally:
        if cleanup is not None:
            cleanup()


def _ocr_via_tempfile(data: bytes, timeout: int) -> OcrResult:
    suffix = _image_suffix_from_data(data)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, prefix="ocr_", delete=False) as tf:
            tf.write(data)
            tmp_path = Path(tf.name)
        return ocr_file(tmp_path, timeout=timeout)
    except OSError as e:
        return OcrResult(ok=False, error=f"OCR tempfile fallback failed: {e}")
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def ocr_bytes(data: bytes, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if not data:
        return OcrResult(ok=False, error="empty image data")
    suffix = _image_suffix_from_data(data)
    if suffix == ".pdf":
        return _ocr_via_tempfile(data, timeout)
    if len(data) * 4 // 3 > _ARGV_BASE64_SAFE_LIMIT:
        return _ocr_via_tempfile(data, timeout)
    b64 = base64.b64encode(data).decode("ascii")
    return ocr_base64(b64, timeout=timeout)


def ocr_base64(b64: str, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if "," in b64 and "base64" in b64[:50]:
        b64 = b64.split(",", 1)[1]
    try:
        decoded = base64.b64decode(b64, validate=True)
    except Exception as e:
        return OcrResult(ok=False, error=f"invalid base64: {e}")
    if _image_suffix_from_data(decoded) == ".pdf":
        return _ocr_via_tempfile(decoded, timeout)
    if len(b64) > _ARGV_BASE64_SAFE_LIMIT:
        return _ocr_via_tempfile(decoded, timeout)
    # 2026-05-18 P167: MinerU primary; Umi-OCR only on hard crash
    if _mineru_available():
        mineru_result = _ocr_via_tempfile(decoded, timeout)
        if mineru_result.ok:
            return mineru_result
        legacy_result = _call_legacy_headless(["--base64", b64], timeout=timeout)
        if legacy_result.ok:
            return legacy_result
        return OcrResult(
            ok=False,
            error=f"MinerU: {mineru_result.error}; fallback: {legacy_result.error}",
        )
    return _call_legacy_headless(["--base64", b64], timeout=timeout)


def is_available() -> bool:
    return _mineru_available() or _legacy_available()


def version() -> str:
    parts: list[str] = []
    if _mineru_available():
        parts.append("mineru")
    about_json = _UMI_DIR / "UmiOCR-data" / "about.json"
    if about_json.is_file():
        try:
            data = json.loads(about_json.read_text(encoding="utf-8"))
            v = data.get("version", {})
            parts.append(f"umi-{v.get('major', 0)}.{v.get('minor', 0)}.{v.get('patch', 0)}")
        except Exception:
            parts.append("umi")
    elif _legacy_available():
        parts.append("umi")
    return "+".join(parts) if parts else "unavailable"


# ──────────────────────────────────────────────────────────────────
# Office (docx/pptx/xlsx) 嵌入图片 OCR (2026-05-16)
# ──────────────────────────────────────────────────────────────────

def ocr_office_images(
    file_path: Union[str, Path],
    *,
    media_prefix: str = "",
    max_images: int = 50,
    max_size_mb: float = 50.0,
    per_image_timeout: int = 90,
) -> dict:
    """提取 office 文件 (docx/pptx/xlsx) 嵌入图片并 OCR.
    
    office 文件本质是 zip:
    - docx: word/media/imageN.{png,jpg,...}
    - pptx: ppt/media/...
    - xlsx: xl/media/...
    
    Args:
        file_path: office 文件路径
        media_prefix: zip 内媒体目录前缀 (留空则按文件类型自动推断)
        max_images: 最多处理几张 (默认 50, 一般足够)
        max_size_mb: 单图最大 MB (默认 50, 几乎不限. 真巨型图才跳)
        per_image_timeout: 单张图 OCR timeout (秒)
    
    Returns:
        {
            "ok": True,
            "total_images": N,         # zip 内总图数
            "ocr_count": M,            # 成功 OCR 的张数
            "skipped_too_large": K,    # 太大跳过的
            "skipped_no_text": J,      # OCR 了但无文本的
            "items": [
                {"name": "image1.png", "size_bytes": 12345, "text": "..."},
                ...
            ],
            "merged_text": "[图 image1.png]\n...\n\n[图 image2.png]\n..."
        }
    """
    import zipfile
    import tempfile
    
    target = Path(file_path)
    if not target.is_file():
        return {"ok": False, "error": f"file not found: {target}"}
    
    # 自动推断 media_prefix
    if not media_prefix:
        suffix = target.suffix.lower()
        if suffix == ".docx":
            media_prefix = "word/media/"
        elif suffix == ".pptx":
            media_prefix = "ppt/media/"
        elif suffix == ".xlsx":
            media_prefix = "xl/media/"
        else:
            return {"ok": False, "error": f"unsupported ext: {suffix}"}
    
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}
    
    items = []
    ocr_count = 0
    skipped_too_large = 0
    skipped_no_text = 0
    total_images = 0
    
    try:
        with zipfile.ZipFile(str(target), "r") as zf:
            media_files = [
                name for name in zf.namelist()
                if name.startswith(media_prefix)
                and any(name.lower().endswith(e) for e in _IMAGE_EXTS)
            ]
            media_files.sort()
            total_images = len(media_files)
            
            if not media_files:
                return {
                    "ok": True, "total_images": 0,
                    "ocr_count": 0, "items": [], "merged_text": "",
                }
            
            for name in media_files:
                if ocr_count >= max_images:
                    break
                
                try:
                    info = zf.getinfo(name)
                    size_bytes = info.file_size
                except KeyError:
                    continue
                
                if size_bytes > max_size_mb * 1024 * 1024:
                    skipped_too_large += 1
                    items.append({
                        "name": os.path.basename(name),
                        "size_bytes": size_bytes,
                        "text": "",
                        "skip_reason": f"too large (>{max_size_mb}MB)",
                    })
                    continue
                
                ext = os.path.splitext(name)[1].lower()
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=ext, prefix="office_img_", delete=False
                    ) as tmp:
                        tmp.write(zf.read(name))
                        tmp_path = tmp.name
                except OSError as e:
                    log.warning("office image extract tempfile failed: %s", e)
                    continue
                
                try:
                    result = ocr_file(Path(tmp_path), timeout=per_image_timeout)
                    text = result.text.strip() if result.ok else ""
                    short_name = os.path.basename(name)
                    if text:
                        items.append({
                            "name": short_name,
                            "size_bytes": size_bytes,
                            "text": text,
                        })
                        ocr_count += 1
                    else:
                        skipped_no_text += 1
                        items.append({
                            "name": short_name,
                            "size_bytes": size_bytes,
                            "text": "",
                            "skip_reason": result.error or "no text detected",
                        })
                except Exception as e:
                    log.warning("ocr image %s failed: %s", name, e)
                finally:
                    if tmp_path:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
    
    except zipfile.BadZipFile:
        return {"ok": False, "error": "not a valid office (zip) file"}
    except Exception as e:
        return {"ok": False, "error": f"unexpected error: {e!r}"}
    
    # 合并 OCR 文本
    merged_parts = []
    for it in items:
        if it.get("text"):
            merged_parts.append(f"[图 {it['name']}]\n{it['text']}")
    merged_text = "\n\n".join(merged_parts)
    
    return {
        "ok": True,
        "total_images": total_images,
        "ocr_count": ocr_count,
        "skipped_too_large": skipped_too_large,
        "skipped_no_text": skipped_no_text,
        "items": items,
        "merged_text": merged_text,
    }
