#!/usr/bin/env python3
"""Generate docs/preview.png, the README hero image.

This script is a *dev tool*, not part of the bot: it needs Pillow
(`pip install Pillow`), which is not a bot dependency and is not listed in
requirements.txt - see README "Dependencies". Nothing here is imported by
alerts_bot/ or main.py.

Usage:
    pip install Pillow
    python3 docs/make_preview.py

It reads the "Volume anomalies" example straight out of README.md (the block
right after the heading that calls it "real output from a `--dry-run` test
run"), so the image can never drift from what the README actually shows.
"""
import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit(
        "Pillow is required to run this script: pip install Pillow\n"
        "(the bot itself does not need it - see README \"Dependencies\")."
    )

DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DOCS_DIR)
README_PATH = os.path.join(REPO_DIR, "README.md")
OUT_PATH = os.path.join(DOCS_DIR, "preview.png")

# Rendered at SCALE x the final size, then downsampled with LANCZOS - cheap
# supersampling so small text stays crisp instead of showing jagged edges.
SCALE = 2
WIDTH, HEIGHT = 1280, 640

# --- palette (clean dark card; no third-party colors/logos) ----------------
BG_TOP = (15, 19, 26)
BG_BOTTOM = (10, 13, 18)
CARD_BORDER = (35, 42, 53)
DIVIDER = (26, 32, 41)
WHITE = (236, 241, 246)
MUTED = (152, 163, 178)
ACCENT = (108, 196, 163)  # soft green "tape" accent, used sparingly
CHIP_BG = (21, 27, 36)
CHIP_BORDER = (57, 67, 81)
CHIP_TEXT = (196, 205, 216)
BUBBLE_BG = (19, 25, 34)
BUBBLE_BORDER = (42, 52, 65)
BUBBLE_HEADER = (232, 237, 242)
BUBBLE_FACT = (210, 218, 227)
BUBBLE_URL = (104, 140, 183)
BUBBLE_FOOTER = (140, 151, 166)

REPO_NAME = "telegram-onchain-alerts"
DESCRIPTION = (
    "Open-source Telegram bot: new pairs & volume anomalies "
    "from public DexScreener data"
)
CHIPS = ["Python stdlib only", "MIT", "83 tests"]

# --- font discovery ----------------------------------------------------------
# No font is bundled in the repo; instead we look for a DejaVu/Inter-like
# sans font and a monospace font in the usual per-OS locations, and fall
# back gracefully (down to PIL's built-in bitmap font) if none exist, so
# this script never crashes for lack of a specific font file.
SANS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]
SANS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\segoeuib.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]
MONO_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "C:\\Windows\\Fonts\\consola.ttf",
    "C:\\Windows\\Fonts\\cour.ttf",
]
MONO_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationMono-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "C:\\Windows\\Fonts\\consolab.ttf",
    "C:\\Windows\\Fonts\\courbd.ttf",
]


def _find_font_path(candidates):
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def make_font_loader(candidates, fallback_candidates=None):
    """Returns a function size -> ImageFont, resolved once, scaled per call."""
    path = _find_font_path(candidates) or _find_font_path(fallback_candidates or [])

    def load(size):
        px = size * SCALE
        if path:
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                pass
        # Graceful fallback: PIL's built-in bitmap font. load_default() got an
        # optional `size` argument in Pillow 10.1; older Pillow ignores it.
        try:
            return ImageFont.load_default(size=px)
        except TypeError:
            return ImageFont.load_default()

    return load


load_sans = make_font_loader(SANS_REGULAR)
load_sans_bold = make_font_loader(SANS_BOLD, SANS_REGULAR)
load_mono = make_font_loader(MONO_REGULAR, SANS_REGULAR)
load_mono_bold = make_font_loader(MONO_BOLD, SANS_BOLD)


# --- pull the example post straight out of README.md ------------------------
def load_example_post():
    marker = "Volume anomalies (real output from a `--dry-run`"
    with open(README_PATH, encoding="utf-8") as fh:
        text = fh.read()
    idx = text.index(marker)  # raises loudly if README's example ever moves/renames
    start = text.index("```", idx)
    start = text.index("\n", start) + 1
    end = text.index("```", start)
    block = text[start:end].rstrip("\n")
    return block.split("\n")


def classify_lines(lines):
    """Tag each line of the example post for styling."""
    tagged = []
    for i, line in enumerate(lines):
        if line.strip() == "":
            tagged.append(("blank", ""))
        elif line.startswith("Tape,"):
            tagged.append(("footer", line))
        elif line.startswith("  ") and "http" in line:
            tagged.append(("url", line.strip()))
        elif i == 0:
            tagged.append(("header", line))
        else:
            tagged.append(("fact", line))
    return tagged


# --- drawing helpers ----------------------------------------------------------
def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if not current or draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def vertical_gradient(draw, box, top, bottom):
    x0, y0, x1, y1 = box
    height = max(y1 - y0, 1)
    for i in range(height):
        t = i / height
        color = tuple(int(top[c] + (bottom[c] - top[c]) * t) for c in range(3))
        draw.line([(x0, y0 + i), (x1, y0 + i)], fill=color)


def fit_mono_size(draw, tagged, max_width, start=17, minimum=10):
    """Largest mono size (header=start, fact=start-2, url/footer=start-4) that
    keeps every line inside max_width, measured with the real chosen font."""
    size = start
    while size > minimum:
        fact_sz, url_sz = size - 2, size - 4
        ok = True
        for kind, text in tagged:
            if kind == "header":
                w = draw.textlength(text, font=load_mono_bold(size))
            elif kind == "fact":
                w = draw.textlength(text, font=load_mono(fact_sz))
            elif kind in ("url", "footer"):
                w = draw.textlength(text, font=load_mono(url_sz))
            else:
                continue
            if w > max_width:
                ok = False
                break
        if ok:
            return size
        size -= 1
    return minimum


def main():
    s = SCALE
    img = Image.new("RGB", (WIDTH * s, HEIGHT * s), BG_TOP)
    draw = ImageDraw.Draw(img)
    vertical_gradient(draw, (0, 0, WIDTH * s, HEIGHT * s), BG_TOP, BG_BOTTOM)

    margin = 3 * s
    draw.rounded_rectangle(
        [margin, margin, WIDTH * s - margin, HEIGHT * s - margin],
        radius=18 * s, outline=CARD_BORDER, width=2 * s,
    )

    divider_x = 592 * s
    draw.line([(divider_x, 72 * s), (divider_x, HEIGHT * s - 72 * s)], fill=DIVIDER, width=1 * s)

    # ---- left column: repo name, description, chips ----
    left_x = 64 * s
    left_max_w = divider_x - left_x - 32 * s

    name_font = load_sans_bold(44)
    name_y = 210 * s
    draw.text((left_x, name_y), REPO_NAME, font=name_font, fill=WHITE)
    name_h = draw.textbbox((0, 0), REPO_NAME, font=name_font)[3]

    desc_font = load_sans(21)
    desc_lines = wrap_text(draw, DESCRIPTION, desc_font, left_max_w)
    desc_line_h = int(21 * 1.5) * s
    desc_y = name_y + name_h + 30 * s
    for i, line in enumerate(desc_lines):
        draw.text((left_x, desc_y + i * desc_line_h), line, font=desc_font, fill=MUTED)

    chip_font = load_sans_bold(15)
    chip_y = desc_y + len(desc_lines) * desc_line_h + 34 * s
    chip_x = left_x
    pad_x, pad_y, gap = 16 * s, 10 * s, 12 * s
    for chip in CHIPS:
        w = draw.textlength(chip, font=chip_font)
        box = [chip_x, chip_y, chip_x + w + 2 * pad_x, chip_y + 20 * s + 2 * pad_y]
        draw.rounded_rectangle(box, radius=(20 * s + 2 * pad_y) / 2, fill=CHIP_BG, outline=CHIP_BORDER, width=1 * s)
        draw.text((chip_x + pad_x, chip_y + pad_y), chip, font=chip_font, fill=CHIP_TEXT)
        chip_x = box[2] + gap

    # ---- right column: Telegram-style channel post bubble ----
    tagged = classify_lines(load_example_post())
    bubble_x0, bubble_x1 = divider_x + 32 * s, WIDTH * s - 64 * s
    inner_w = bubble_x1 - bubble_x0 - 2 * 26 * s
    mono_size = fit_mono_size(draw, tagged, inner_w)
    fact_size, url_size = mono_size - 2, mono_size - 4

    header_font = load_mono_bold(mono_size)
    fact_font = load_mono(fact_size)
    url_font = load_mono(url_size)
    footer_font = load_mono(url_size)

    def line_height(kind):
        if kind == "header":
            return int(mono_size * 1.55) * s
        if kind == "blank":
            return int(mono_size * 0.85) * s
        return int((fact_size if kind == "fact" else url_size) * 1.55) * s

    content_h = sum(line_height(k) for k, _ in tagged)
    pad_y = 26 * s
    bubble_h = content_h + 2 * pad_y
    bubble_y0 = (HEIGHT * s - bubble_h) // 2
    bubble_y1 = bubble_y0 + bubble_h

    draw.rounded_rectangle(
        [bubble_x0, bubble_y0, bubble_x1, bubble_y1],
        radius=16 * s, fill=BUBBLE_BG, outline=BUBBLE_BORDER, width=1 * s,
    )

    text_x = bubble_x0 + 26 * s
    cursor_y = bubble_y0 + pad_y
    for kind, text in tagged:
        h = line_height(kind)
        if kind == "header":
            draw.text((text_x, cursor_y), text, font=header_font, fill=BUBBLE_HEADER)
        elif kind == "fact":
            # Bullet in the accent color - a small "signal" touch - then the
            # rest of the line in the normal fact color.
            bullet, rest = text[:1], text[1:]
            draw.text((text_x, cursor_y), bullet, font=fact_font, fill=ACCENT)
            draw.text((text_x + draw.textlength(bullet, font=fact_font), cursor_y),
                       rest, font=fact_font, fill=BUBBLE_FACT)
        elif kind == "url":
            draw.text((text_x, cursor_y), text, font=url_font, fill=BUBBLE_URL)
        elif kind == "footer":
            sep_y = cursor_y - 4 * s
            draw.line([(text_x, sep_y), (bubble_x1 - 26 * s, sep_y)], fill=BUBBLE_BORDER, width=1 * s)
            draw.text((text_x, cursor_y + 6 * s), text, font=footer_font, fill=BUBBLE_FOOTER)
        cursor_y += h

    final = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    final.save(OUT_PATH, format="PNG", optimize=True)
    print("wrote %s (%dx%d)" % (OUT_PATH, WIDTH, HEIGHT))


if __name__ == "__main__":
    main()
