from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from openai import AsyncOpenAI

from app.config import settings
from app.llm.tools.delegate_quality import repair_common_mojibake_text


_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_GATEWAY_MOJIBAKE_HINTS = (
    "\u5a34\u5b2d",  # common damage for Chinese UTF-8 bytes decoded as GBK
    "\u762f\u9365",
    "\u60e7\u511a",
    "\u9225\u6a9a",  # curly apostrophe damage
    "\u9225\u6dc0",
    "\u7481\u6218",
    "\u934f\u5d87",
)


def _data_uri_from_path(path: str | Path) -> str:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"image file not found: {source}")
    data = source.read_bytes()
    if not data:
        raise ValueError("image file is empty")
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError("image exceeds the 20 MiB model-vision limit")
    mime = mimetypes.guess_type(source.name)[0] or "image/png"
    if not mime.startswith("image/"):
        raise ValueError(f"model vision only accepts image files, got {source.suffix or 'unknown'}")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _normalize_data_uri(image_base64: str) -> str:
    value = str(image_base64 or "").strip()
    if value.startswith("data:image/") and ";base64," in value[:100]:
        return value
    compact = "".join(value.split())
    raw = base64.b64decode(compact, validate=True)
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError("invalid or oversized base64 image")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("base64 content is not a supported PNG/JPEG/WebP image")
    return f"data:{mime};base64,{compact}"


def _response_text(response) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    content = getattr(getattr(choices[0], "message", None), "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
            else:
                text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _repair_response_mojibake(text: str) -> tuple[str, int]:
    repaired_lines: list[str] = []
    repaired_count = 0
    for line in str(text or "").splitlines():
        repaired, info = repair_common_mojibake_text(line)
        if info is None and any(hint in line for hint in _GATEWAY_MOJIBAKE_HINTS):
            for encoding in ("gbk", "cp936"):
                try:
                    candidate = line.encode(encoding, errors="strict").decode("utf-8", errors="strict")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if candidate != line:
                    repaired = candidate
                    info = {"encoding": encoding}
                    break
        repaired_lines.append(repaired)
        if info is not None:
            repaired_count += 1
    return "\n".join(repaired_lines).strip(), repaired_count


async def describe_image(
    *,
    image_path: str | Path | None = None,
    image_base64: str = "",
    purpose: str = "",
    require_feature_enabled: bool = True,
) -> dict:
    if require_feature_enabled and not settings.model_vision_enabled:
        return {"ok": False, "error": "model vision is disabled"}
    if not settings.gpt55_api_key or not settings.gpt55_base_url:
        return {"ok": False, "error": "GPT55 API configuration is missing"}
    try:
        data_uri = _data_uri_from_path(image_path) if image_path else _normalize_data_uri(image_base64)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    task = str(purpose or "").strip()
    prompt = (
        "Inspect this image carefully and return a detailed, evidence-grounded description. "
        "Cover the scene/subject, layout and spatial relationships, visible objects and people, "
        "colors, style, lighting, notable details, and all legible text exactly as seen. "
        "Separate direct observations from uncertain interpretations; never invent obscured content."
    )
    if task:
        prompt += f"\nUser purpose: {task[:2000]}"
    try:
        client = AsyncOpenAI(
            api_key=settings.gpt55_api_key,
            base_url=settings.gpt55_base_url.rstrip("/"),
            timeout=180.0,
        )
        response = await client.chat.completions.create(
            model=settings.model_vision_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                ],
            }],
        )
        text, repaired_lines = _repair_response_mojibake(_response_text(response))
        if not text:
            return {"ok": False, "error": "model vision returned no description"}
        return {
            "ok": True,
            "text": text,
            "description": text,
            "engine": "model_vision",
            "model": settings.model_vision_model,
            "encoding_repaired_lines": repaired_lines,
        }
    except Exception as exc:
        return {"ok": False, "error": f"model vision request failed: {type(exc).__name__}: {exc}"}
