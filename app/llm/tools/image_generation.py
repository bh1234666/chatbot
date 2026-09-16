from __future__ import annotations

import base64
import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.llm.tools.model_vision import describe_image


@dataclass
class GeneratedImage:
    ok: bool
    path: str = ""
    mode: str = ""
    description: str = ""
    model: str = ""
    width: int = 0
    height: int = 0
    bytes: int = 0
    sha256: str = ""
    revised_prompt: str = ""
    error: str = ""


def _extract_image_payload(response) -> tuple[bytes | None, str, str]:
    data = getattr(response, "data", None) or []
    if not data:
        return None, "", ""
    item = data[0]
    b64_value = getattr(item, "b64_json", None) or (item.get("b64_json") if isinstance(item, dict) else None)
    url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None) or ""
    revised = getattr(item, "revised_prompt", None) or (item.get("revised_prompt") if isinstance(item, dict) else None) or ""
    if b64_value:
        return base64.b64decode(b64_value), "", str(revised)
    return None, str(url), str(revised)


def _chat_content(response) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    value = getattr(getattr(choices[0], "message", None), "content", "")
    return value if isinstance(value, str) else str(value or "")


def _image_ref_from_text(text: str) -> str:
    data_match = re.search(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", text)
    if data_match:
        return data_match.group(0).replace("\n", "").replace(" ", "")
    url_match = re.search(r"https?://[^\s)\]>'\"]+", text)
    return url_match.group(0) if url_match else ""


async def _download_image(url: str) -> bytes:
    if url.startswith("data:image/") and ";base64," in url[:100]:
        return base64.b64decode(url.split(",", 1)[1])
    headers = {}
    base_host = urlparse(settings.gpt55_base_url).netloc
    if urlparse(url).netloc == base_host:
        headers["Authorization"] = f"Bearer {settings.gpt55_api_key}"
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
        response = await http.get(url, headers=headers)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if content_type and not content_type.startswith("image/"):
            raise ValueError(f"generation URL returned non-image content: {content_type}")
        return response.content


def _dimensions(data: bytes) -> tuple[int, int, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width, height, "png"
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return width, height, "jpeg"
            if index + 4 > len(data):
                break
            length = struct.unpack(">H", data[index + 2:index + 4])[0]
            index += max(2, length + 2)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        if data[12:16] == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height, "webp"
        return 1, 1, "webp"
    raise ValueError("API response is not a supported PNG/JPEG/WebP image")


async def generate_image(
    *,
    prompt: str,
    output_path: str | Path,
    input_image: str | Path | None = None,
    size: str = "1024x1024",
) -> GeneratedImage:
    if not settings.image_generation_enabled:
        return GeneratedImage(ok=False, error="AI image generation is disabled")
    if not settings.gpt55_api_key or not settings.gpt55_base_url:
        return GeneratedImage(ok=False, error="GPT55 API configuration is missing")
    prompt = str(prompt or "").strip()
    if not prompt:
        return GeneratedImage(ok=False, error="generation prompt is empty")
    if len(prompt) > 12000:
        return GeneratedImage(ok=False, error="generation prompt exceeds 12000 characters")
    source = Path(input_image) if input_image else None
    if source is not None and (not source.is_file() or source.stat().st_size > 20 * 1024 * 1024):
        return GeneratedImage(ok=False, error="reference image is missing or exceeds 20 MiB")
    if source is not None:
        try:
            ref_width, ref_height, _ = _dimensions(source.read_bytes())
            if ref_width < 32 or ref_height < 32:
                raise ValueError(f"reference image is too small: {ref_width}x{ref_height}")
        except Exception as exc:
            return GeneratedImage(ok=False, error=f"invalid reference image: {exc}")

    mode = "image-to-image" if source else "text-to-image"
    client = AsyncOpenAI(
        api_key=settings.gpt55_api_key,
        base_url=settings.gpt55_base_url.rstrip("/"),
        timeout=180.0,
    )
    image_bytes: bytes | None = None
    image_url = ""
    revised_prompt = ""
    primary_error = ""
    try:
        if source:
            with source.open("rb") as stream:
                response = await client.images.edit(
                    model=settings.image_generation_model,
                    image=stream,
                    prompt=prompt,
                    size=size,
                )
        else:
            response = await client.images.generate(
                model=settings.image_generation_model,
                prompt=prompt,
                size=size,
            )
        image_bytes, image_url, revised_prompt = _extract_image_payload(response)
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc}"

    if image_bytes is None and not image_url:
        try:
            if source:
                mime = "image/png"
                encoded = base64.b64encode(source.read_bytes()).decode("ascii")
                content = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ]
            else:
                content = prompt
            response = await client.chat.completions.create(
                model=settings.image_generation_model,
                messages=[{"role": "user", "content": content}],
            )
            image_url = _image_ref_from_text(_chat_content(response))
        except Exception as exc:
            fallback_error = f"{type(exc).__name__}: {exc}"
            return GeneratedImage(
                ok=False,
                mode=mode,
                model=settings.image_generation_model,
                error=f"image API failed ({primary_error}); chat fallback failed ({fallback_error})",
            )
    try:
        if image_bytes is None:
            if not image_url:
                raise ValueError("API returned no extractable image")
            image_bytes = await _download_image(image_url)
        if len(image_bytes) < 1024 or len(image_bytes) > 50 * 1024 * 1024:
            raise ValueError("generated image size is outside the valid range")
        width, height, image_format = _dimensions(image_bytes)
        if Path(output_path).suffix.lower() == ".png" and image_format != "png":
            raise ValueError(f"image model returned {image_format}, but PNG output was requested")
        if width < 256 or height < 256:
            raise ValueError(f"generated image dimensions are too small: {width}x{height}")
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(image_bytes)
        vision = await describe_image(
            image_path=destination,
            purpose=(
                "Describe the generated image for delivery and compare its visible content with this generation request: "
                + prompt[:3000]
            ),
            require_feature_enabled=False,
        )
        description = vision.get("description", "") if vision.get("ok") else ""
        return GeneratedImage(
            ok=True,
            path=str(destination),
            mode=mode,
            description=description,
            model=settings.image_generation_model,
            width=width,
            height=height,
            bytes=len(image_bytes),
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            revised_prompt=revised_prompt,
        )
    except Exception as exc:
        return GeneratedImage(ok=False, mode=mode, model=settings.image_generation_model, error=str(exc))
