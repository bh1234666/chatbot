# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import math
import random
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

try:
    import numpy as np
except Exception:
    np = None

CANVAS = (1400, 1900)
BG = (250, 250, 248)
INK = (25, 25, 25)

DISPLAY_FORMULAS = {
    "zh_en_mixed_calculus": [
        r"I=\int_0^{+\infty} e^{-x^2}\,dx",
        r"\iint_D (x^2+y^2)\,dx\,dy,\quad D:x^2+y^2\leq1",
        r"\lim_{x\to0}\frac{\sin x-x\cos x}{x^3}",
        r"\det(A-\lambda I),\quad A=\left(\frac{1\;2}{3\;4}\right)",
    ],
    "complex_integrals": [
        r"\iint_S (\nabla\times F)\cdot n\,dS=\oint_{\partial S}F\cdot dr",
        r"\int_{-\infty}^{\infty} e^{-a x^2}\cos(bx)\,dx=\sqrt{\pi/a}\,e^{-b^2/(4a)}",
        r"\iiint_\Omega \operatorname{div}F\,dV=\iint_{\partial\Omega}F\cdot n\,dS",
    ],
    "exam_blanks": [
        r"f'(x)=2x\Rightarrow f(x)=x^2+C",
        r"\int x\cos x\,dx=x\sin x+\cos x+C",
        r"\sum_{k=1}^{n}k=\frac{n(n+1)}{2}",
        r"\oint_C(x\,dy-y\,dx)=2\pi",
    ],
    "multilingual_text_math": [
        r"F(\omega)=\int_{-\infty}^{\infty} f(t)e^{-i\omega t}\,dt",
        r"\alpha>0,\quad \int_0^\infty e^{-\alpha x}\,dx=\frac{1}{\alpha}",
        r"\lim_{n\to\infty}\left(1+\frac{1}{n}\right)^n=e",
    ],
}

TEXT_CASES = [
    {
        "id": "zh_en_mixed_calculus",
        "lang": "ch",
        "layout": "single",
        "title": "高等数学练习 / Calculus Practice",
        "lines": [
            "1. 设 f(x)=exp(-x^2)，计算下面公式框中的高斯积分，并说明收敛性。",
            "2. 求单位圆盘 D 上的二重积分，D 由 x^2+y^2≤1 给出。",
            "3. Evaluate the limit shown below as x tends to 0.",
            "4. 若 A=[[1,2],[3,4]]，求特征方程 det(A-λI)=0。",
        ],
        "formulas": ["\\int_0^{+\\infty} e^{-x^2} dx", "\\iint_D (x^2+y^2)\\,dxdy", "\\lim_{x\\to0} \\frac{\\sin x-x\\cos x}{x^3}", "\\det(A-\\lambda I)"],
    },
    {
        "id": "complex_integrals",
        "lang": "en",
        "layout": "single",
        "title": "Complex integral forms",
        "lines": [
            "Compute the following vector-calculus integrals. The exact notation is printed in the formula box.",
            "(a) Stokes theorem on an oriented surface S.",
            "(b) Fourier-type Gaussian integral over the real line.",
            "(c) Divergence theorem on a three-dimensional region Ω.",
        ],
        "formulas": ["\\iint_S (\\nabla\\times F)\\cdot n\\,dS", "\\int_{-\\infty}^{\\infty} e^{-a x^2}\\cos(bx)\\,dx", "\\iiint_\\Omega \\operatorname{div}F\\,dV"],
    },
    {
        "id": "exam_blanks",
        "lang": "ch",
        "layout": "two_column",
        "title": "数学试卷 OCR 压力测试",
        "lines": [
            "一、填空题：",
            "1. 若导函数为 2x，请写出一个原函数。",
            "2. 计算 x cos x 的不定积分。",
            "二、解答题：",
            "3. 证明前 n 个正整数之和公式。",
            "4. 求单位圆 C 上的曲线积分。",
        ],
        "formulas": ["f'(x)=2x", "\\int x\\cos x\\,dx", "\\sum_{k=1}^{n} k", "\\oint_C (x\\,dy-y\\,dx)"],
    },
    {
        "id": "multilingual_text_math",
        "lang": "latin",
        "layout": "single",
        "title": "Multilingual notes",
        "lines": [
            "English: The Fourier transform is defined in the formula box below.",
            "Deutsch: Für positives alpha gilt die angegebene Exponentialintegral-Identität.",
            "Français: La limite classique de la suite est fondamentale.",
            "中文：当 x 趋近 0 时，sin x / x 趋近 1。",
        ],
        "formulas": ["F(\\omega)=\\int f(t)e^{-i\\omega t}dt", "\\int_0^\\infty e^{-\\alpha x}dx=1/\\alpha", "\\lim_{n\\to\\infty}(1+1/n)^n=e"],
    },
]

DEGRADES = {
    "clean": {},
    "rotated": {"rotate": 2.5},
    "blurred": {"blur": 1.2, "contrast": 0.92},
    "noisy_jpeg": {"noise": 8, "jpeg": 62},
    "perspective": {"perspective": 0.035, "rotate": -1.5},
    "hard": {"rotate": 2.0, "blur": 1.0, "noise": 10, "contrast": 0.86, "jpeg": 55, "perspective": 0.025},
}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/times.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def configure_matplotlib_fonts() -> None:
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/times.ttf",
    ]
    families = []
    for path in candidates:
        if Path(path).is_file():
            font_manager.fontManager.addfont(path)
            families.append(font_manager.FontProperties(fname=path).get_name())
    if families:
        plt.rcParams["font.family"] = families
    plt.rcParams["mathtext.fontset"] = "dejavuserif"


def render_formula(formula: str, *, dpi: int = 220) -> Image.Image:
    fig = plt.figure(figsize=(1, 1), dpi=dpi)
    fig.patch.set_alpha(0)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    text = ax.text(0, 0, f"${formula}$", fontsize=22, color=(0, 0, 0), va="bottom", ha="left")
    fig.canvas.draw()
    bbox = text.get_window_extent(renderer=fig.canvas.get_renderer()).expanded(1.04, 1.25)
    fig.set_size_inches(bbox.width / dpi, bbox.height / dpi)
    ax.set_position([0, 0, 1, 1])
    text.set_position((0.02, 0.08))
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, transparent=True, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGBA")


def paste_formula(canvas: Image.Image, formula: str, x: int, y: int, max_width: int) -> int:
    rendered = render_formula(formula)
    if rendered.width > max_width:
        scale = max_width / rendered.width
        rendered = rendered.resize((max(1, int(rendered.width * scale)), max(1, int(rendered.height * scale))), Image.Resampling.LANCZOS)
    canvas.alpha_composite(rendered, (x, y))
    return y + rendered.height + 28


def wrap_line(draw: ImageDraw.ImageDraw, text: str, max_width: int, fnt) -> list[str]:
    chunks: list[str] = []
    current = ""
    for ch in text:
        trial = current + ch
        if draw.textlength(trial, font=fnt) <= max_width or not current:
            current = trial
        else:
            chunks.append(current)
            current = ch
    if current:
        chunks.append(current)
    return chunks


def draw_case(case: dict, seed: int) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGBA", CANVAS, BG + (255,))
    draw = ImageDraw.Draw(img)
    title_font = font(44, bold=True)
    body_font = font(34)
    margin = 90
    y = 80
    draw.text((margin, y), case["title"], fill=INK + (255,), font=title_font)
    y += 85

    if case["layout"] == "two_column":
        col_w = (CANVAS[0] - margin * 2 - 50) // 2
        positions = [(margin, y), (margin + col_w + 50, y)]
        for idx, line in enumerate(case["lines"]):
            col = 0 if idx < math.ceil(len(case["lines"]) / 2) else 1
            base_x, base_y = positions[col]
            local_idx = idx if col == 0 else idx - math.ceil(len(case["lines"]) / 2)
            yy = base_y + local_idx * 112
            for part in wrap_line(draw, line, col_w, body_font):
                draw.text((base_x, yy), part, fill=INK + (255,), font=body_font)
                yy += 42
        box_y = 760
    else:
        for line in case["lines"]:
            for part in wrap_line(draw, line, CANVAS[0] - margin * 2, body_font):
                draw.text((margin, y), part, fill=INK + (255,), font=body_font)
                y += 48
            y += 28
        box_y = max(y + 30, 820)

    formulas = DISPLAY_FORMULAS[case["id"]]
    box_bottom = min(CANVAS[1] - 120, box_y + 520)
    draw.rounded_rectangle((margin, box_y, CANVAS[0] - margin, box_bottom), radius=18, outline=(130, 130, 130, 255), width=2)
    yy = box_y + 34
    for formula in formulas:
        yy = paste_formula(img, formula, margin + 34, yy, CANVAS[0] - margin * 2 - 68)

    draw = ImageDraw.Draw(img)
    for _ in range(10):
        x = rng.randint(60, CANVAS[0] - 60)
        y0 = rng.randint(70, CANVAS[1] - 70)
        shade = rng.randint(225, 245)
        draw.line((x, y0, x + rng.randint(-30, 30), y0 + rng.randint(-10, 10)), fill=(shade, shade, shade, 255), width=1)
    return img.convert("RGB")


def perspective(img: Image.Image, amount: float) -> Image.Image:
    w, h = img.size
    dx = int(w * amount)
    dy = int(h * amount)
    coeffs = find_coeffs(
        [(0, 0), (w, 0), (w, h), (0, h)],
        [(dx, dy), (w - dx, 0), (w, h - dy), (0, h)],
    )
    return img.transform((w, h), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC, fillcolor=BG)


def find_coeffs(pa, pb):
    if np is None:
        return (1, 0, 0, 0, 1, 0, 0, 0)
    matrix = []
    for p1, p2 in zip(pa, pb):
        matrix.append([p1[0], p1[1], 1, 0, 0, 0, -p2[0] * p1[0], -p2[0] * p1[1]])
        matrix.append([0, 0, 0, p1[0], p1[1], 1, -p2[1] * p1[0], -p2[1] * p1[1]])
    a = np.matrix(matrix, dtype=float)
    b = np.array(pb).reshape(8)
    return tuple(float(x) for x in np.linalg.solve(a, b).tolist())


def degrade(img: Image.Image, opts: dict, out_path: Path) -> None:
    if opts.get("perspective"):
        img = perspective(img, float(opts["perspective"]))
    if opts.get("rotate"):
        img = img.rotate(float(opts["rotate"]), resample=Image.Resampling.BICUBIC, expand=False, fillcolor=BG)
    if opts.get("blur"):
        img = img.filter(ImageFilter.GaussianBlur(float(opts["blur"])))
    if opts.get("contrast"):
        img = ImageEnhance.Contrast(img).enhance(float(opts["contrast"]))
    if opts.get("noise") and np is not None:
        arr = np.asarray(img).astype("int16")
        noise = np.random.default_rng(123).normal(0, float(opts["noise"]), arr.shape)
        arr = np.clip(arr + noise, 0, 255).astype("uint8")
        img = Image.fromarray(arr, "RGB")
    if opts.get("jpeg"):
        jpg = out_path.with_suffix(".jpg")
        img.save(jpg, quality=int(opts["jpeg"]), optimize=True)
        Image.open(jpg).save(out_path)
        jpg.unlink(missing_ok=True)
    else:
        img.save(out_path)


def main() -> None:
    configure_matplotlib_fonts()
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/ocr_benchmark")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    out_dir = Path(args.out)
    image_dir = out_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    count = 0
    for case in TEXT_CASES:
        base = draw_case(case, seed=count + 1)
        for degrade_name, opts in DEGRADES.items():
            case_id = f"{case['id']}__{degrade_name}"
            path = image_dir / f"{case_id}.png"
            degrade(base.copy(), opts, path)
            rows.append({
                "id": case_id,
                "image": str(path.as_posix()),
                "lang": case["lang"],
                "layout": case["layout"],
                "degrade": degrade_name,
                "text": "\n".join([case["title"], *case["lines"]]),
                "formulas": case["formulas"],
            })
            count += 1
            if args.limit and count >= args.limit:
                break
        if args.limit and count >= args.limit:
            break

    manifest = out_dir / "manifest.jsonl"
    with manifest.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"generated {len(rows)} cases under {out_dir}")


if __name__ == "__main__":
    main()
