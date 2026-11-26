"""Title/problem/architecture/outro cards, drawn with Pillow (1920x1080 PNG). No external assets."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
BG, FG, DIM, GREEN, RED, BLUE, AMBER = "#1c1d29", "#f2f3f8", "#9aa0b5", "#50fa7b", "#ff6b6b", "#7aa2f7", "#ffb86c"
FONTS = Path("/usr/share/fonts/truetype/dejavu")


def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    name = ("DejaVuSansMono" if mono else "DejaVuSans") + ("-Bold" if bold else "") + ".ttf"
    return ImageFont.truetype(str(FONTS / name), size)


def wrap(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = f"{cur} {w}".strip()
        if d.textlength(t, font=f) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + [cur]


def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGB", (W, H), BG)
    return im, ImageDraw.Draw(im)


def title_card(path: Path, title: str, tagline: str, small: str) -> None:
    im, d = canvas()
    d.text((W // 2, 400), title, font=font(150, True), fill=GREEN, anchor="mm")
    y = 560
    for line in wrap(d, tagline, font(52), 1500):
        d.text((W // 2, y), line, font=font(52), fill=FG, anchor="mm")
        y += 70
    d.text((W // 2, H - 110), small, font=font(30), fill=DIM, anchor="mm")
    im.save(path)


def bullets_card(path: Path, kicker: str, heading: str, bullets: list[str]) -> None:
    im, d = canvas()
    d.text((160, 170), kicker, font=font(34, True), fill=AMBER)
    y = 240
    for line in wrap(d, heading, font(76, True), 1600):
        d.text((160, y), line, font=font(76, True), fill=FG)
        y += 98
    y += 40
    for b in bullets:
        lines = wrap(d, b, font(46), 1520)
        d.text((160, y), "•", font=font(46), fill=RED)
        for i, line in enumerate(lines):
            d.text((220, y), line, font=font(46), fill=FG)
            y += 64
        y += 34
    im.save(path)


def box(d: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], title: str, lines: list[str], color: str) -> None:
    d.rounded_rectangle(xy, radius=22, outline=color, width=5, fill="#23243a")
    x0, y0, x1, _ = xy
    d.text(((x0 + x1) // 2, y0 + 46), title, font=font(38, True), fill=color, anchor="mm")
    y = y0 + 100
    for line in lines:
        d.text(((x0 + x1) // 2, y), line, font=font(28), fill=FG, anchor="mm")
        y += 40


def arrow(d: ImageDraw.ImageDraw, a: tuple[int, int], b: tuple[int, int], label: str, color: str, above: bool = True) -> None:
    d.line([a, b], fill=color, width=6)
    dx = 1 if b[0] > a[0] else -1 if b[0] < a[0] else 0
    dy = 1 if b[1] > a[1] else -1 if b[1] < a[1] else 0
    tip = b
    if dx:
        d.polygon([tip, (tip[0] - 28 * dx, tip[1] - 16), (tip[0] - 28 * dx, tip[1] + 16)], fill=color)
    else:
        d.polygon([tip, (tip[0] - 16, tip[1] - 28 * dy), (tip[0] + 16, tip[1] - 28 * dy)], fill=color)
    mx, my = (a[0] + b[0]) // 2, (a[1] + b[1]) // 2
    d.text((mx, my - 34 if above else my + 34), label, font=font(26), fill=color, anchor="mm")


def architecture_card(path: Path) -> None:
    im, d = canvas()
    d.text((160, 90), "THE IDEA", font=font(34, True), fill=AMBER)
    d.text((160, 140), "The agent proposes. A human signs.", font=font(60, True), fill=FG)
    d.text((160, 220), "Flagsmith's own flag engine shows who would change, before anything happens.", font=font(38), fill=DIM)
    box(d, (110, 340, 590, 660), "Coding agent", ["Codex CLI via MCP (fg-mcp)", "", "holds NO Flagsmith key", "cannot apply or approve"], BLUE)
    box(d, (720, 300, 1200, 700), "Gatekeeper", ["the only holder of the key", "diff + impact preview", "(flag engine, 200 identities)", "guardrails, drift check", "hash-chained audit log"], GREEN)
    box(d, (1330, 340, 1810, 660), "Flagsmith", ["self-hosted v2.295.2", "Admin API, environment", "document, SDK"], AMBER)
    arrow(d, (590, 500), (720, 500), "plan", BLUE)
    box(d, (720, 810, 1200, 1030), "Human", ["fg approve: Ed25519 signature", "bound to the plan hash"], RED)
    arrow(d, (960, 810), (960, 700), "signed token", RED, above=False)
    arrow(d, (1200, 500), (1330, 500), "apply", GREEN)
    im.save(path)


def outro_card(path: Path, lines: list[str], small: str) -> None:
    im, d = canvas()
    d.text((W // 2, 250), "FlagGate", font=font(110, True), fill=GREEN, anchor="mm")
    y = 420
    for line in lines:
        for l2 in wrap(d, line, font(46), 1560):
            d.text((W // 2, y), l2, font=font(46), fill=FG, anchor="mm")
            y += 64
        y += 26
    d.text((W // 2, H - 100), small, font=font(30), fill=DIM, anchor="mm")
    im.save(path)


if __name__ == "__main__":      # python cards.py <outdir>: draw every card of the storyboard as card-<key>.png
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import storyboard as sb
    out = Path(sys.argv[1])
    for key, _ in sb.CARDS_BEFORE + sb.CARDS_AFTER:
        sb.draw_card(key, out / f"card-{key}.png", sys.modules[__name__])
