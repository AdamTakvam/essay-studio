"""Renders slide/scene still images with Pillow (no external image service needed)."""
import functools
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080

THEMES = {
    "expository": {"bg": ((15, 23, 42), (30, 58, 95)), "accent": (56, 189, 248), "text": (241, 245, 249),
                   "muted": (148, 163, 184)},
    "narrative": {"bg": ((28, 16, 36), (88, 40, 60)), "accent": (251, 191, 36), "text": (254, 243, 228),
                  "muted": (214, 180, 160)},
}


@functools.lru_cache(maxsize=None)
def _font_file(style: str) -> str | None:
    for pattern in (f"Inter:style={style}", f"DejaVu Sans:style={style}", f"sans-serif:style={style}", "sans-serif"):
        try:
            path = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
            if path and Path(path).exists():
                return path
        except Exception:
            break
    return None


def font(size: int, style: str = "Regular") -> ImageFont.FreeTypeFont:
    f = _font_file(style)
    try:
        return ImageFont.truetype(f, size) if f else ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default(size)


def _background(theme: dict) -> Image.Image:
    top, bottom = theme["bg"]
    img = Image.new("RGB", (W, H), top)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    # soft accent glow
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W - 700, -300, W + 300, 600], fill=tuple(c // 3 for c in theme["accent"]))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    return Image.blend(img, Image.composite(glow, img, glow.convert("L")), 0.55)


def _wrap(draw, text, fnt, max_w):
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            line = trial
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def render_slide(out: Path, *, kind: str, heading: str, bullets: list[str] | None = None,
                 caption: str = "", index: int = 0, total: int = 0, deck_title: str = "",
                 is_title: bool = False) -> Path:
    theme = THEMES.get(kind, THEMES["expository"])
    img = _background(theme)
    d = ImageDraw.Draw(img)
    margin = 140

    if is_title:
        tag = "AN EXPLAINER" if kind == "expository" else "A STORY"
        d.text((margin, 330), tag, font=font(34, "Bold"), fill=theme["accent"])
        y = 400
        for line in _wrap(d, heading, font(96, "Bold"), W - 2 * margin)[:4]:
            d.text((margin, y), line, font=font(96, "Bold"), fill=theme["text"])
            y += 116
        if caption:
            for line in _wrap(d, caption, font(42), W - 2 * margin)[:3]:
                d.text((margin, y + 30), line, font=font(42), fill=theme["muted"])
                y += 58
        d.rectangle([margin, 300, margin + 120, 308], fill=theme["accent"])
    else:
        d.rectangle([0, 0, 18, H], fill=theme["accent"])
        label = f"{index:02d}" + (f" / {total:02d}" if total else "")
        d.text((margin, 110), label, font=font(30, "Bold"), fill=theme["accent"])
        y = 170
        hfont = font(78, "Bold")
        for line in _wrap(d, heading, hfont, W - 2 * margin)[:3]:
            d.text((margin, y), line, font=hfont, fill=theme["text"])
            y += 96
        y += 40
        if bullets:
            bfont = font(46)
            for b in bullets[:5]:
                lines = _wrap(d, b, bfont, W - 2 * margin - 70)[:3]
                d.ellipse([margin, y + 20, margin + 18, y + 38], fill=theme["accent"])
                for line in lines:
                    d.text((margin + 60, y), line, font=bfont, fill=theme["text"])
                    y += 62
                y += 26
                if y > H - 160:
                    break
        if caption:
            cfont = font(54, "Italic")
            lines = _wrap(d, f"“{caption}”", cfont, W - 2 * margin)[:4]
            cy = max(y, H - 140 - len(lines) * 70)
            for line in lines:
                d.text((margin, cy), line, font=cfont, fill=theme["muted"])
                cy += 70
    if deck_title and not is_title:
        d.text((margin, H - 80), textwrap.shorten(deck_title, 90), font=font(26), fill=theme["muted"])
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


def render_card(out: Path, title: str, subtitle: str, kind: str = "expository", size=(W, H)) -> Path:
    """A simple title card (used for audio-only videos when packaging)."""
    p = render_slide(out, kind=kind, heading=title, caption=subtitle, is_title=True)
    if size != (W, H):
        img = Image.open(p)
        bg = img.resize((int(size[1] * W / H), size[1])).filter(ImageFilter.GaussianBlur(30))
        canvas = Image.new("RGB", size)
        canvas.paste(bg, ((size[0] - bg.width) // 2, 0))
        fg = img.resize((size[0], int(size[0] * H / W)))
        canvas.paste(fg, (0, (size[1] - fg.height) // 2))
        canvas.save(p)
    return p
