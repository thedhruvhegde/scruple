"""writes every logo svg in assets/logos from the mark geometry and the outlined wordmark."""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WM = json.load(open(os.path.join(ROOT, "tools", "wordmark.json")))

# colourways: background, ink, weight
WAYS = {
    "pine-on-bone":  ("#EDEAE1", "#1D4A3A", "#B8892E"),
    "bone-on-pine":  ("#12301F", "#EDEAE1", "#E8C46A"),
    "bone-on-night": ("#0E1626", "#E6E3D6", "#E8C46A"),
    "ink-on-sand":   ("#E3D8BF", "#17140C", "#A63A1D"),
    "night-on-brass": ("#E8C46A", "#0E1626", "#F4F1E8"),
}

def mark(ink, weight):
    # 24 x 24 grid: a beam tipped by one small weight, resting on a fulcrum
    return (f'<g transform="rotate(-6 12 11)"><rect x="1.5" y="10" width="21" height="2.4" fill="{ink}"/>'
            f'<rect x="3" y="5.4" width="4.6" height="4.6" fill="{weight}"/></g>'
            f'<path d="M12 12.6L17.5 21.5H6.5Z" fill="{ink}"/>')

def word(ink, x, y, h):
    # place the outlined wordmark so its x-height spans h units
    s = h / WM["xh"]
    return f'<path transform="translate({x} {y}) scale({s})" d="{WM["d"]}" fill="{ink}"/>', WM["w"] * s

def svg(w, h, body, bg):
    rect = f'<rect width="{w}" height="{h}" fill="{bg}"/>' if bg else ""
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">{rect}{body}</svg>\n'

def lockups(bg, ink, weight):
    out = {}
    out["mark"] = svg(512, 512, f'<g transform="translate(96 96) scale(13.333)">{mark(ink, weight)}</g>', bg)
    # horizontal: mark height 120, wordmark x-height matched to the beam-to-base span
    wd, ww = word(ink, 0, 0, 1)
    s = 64 / WM["xh"]; wwidth = WM["w"] * s
    body = (f'<g transform="translate(60 40) scale(5)">{mark(ink, weight)}</g>'
            f'<path transform="translate(200 {40 + 120 - 8 - WM["asc"] * s}) scale({s})" d="{WM["d"]}" fill="{ink}"/>')
    out["horizontal"] = svg(int(200 + wwidth + 60), 200, body, bg)
    s2 = 80 / WM["xh"]; w2 = WM["w"] * s2; W2 = int(max(w2, 240) + 120)
    body = (f'<g transform="translate({(W2 - 240) / 2} 50) scale(10)">{mark(ink, weight)}</g>'
            f'<path transform="translate({(W2 - w2) / 2} {50 + 240 + 40 + 80 - WM["asc"] * s2}) scale({s2})" d="{WM["d"]}" fill="{ink}"/>')
    out["stacked"] = svg(W2, 50 + 240 + 40 + 80 + 90, body, bg)
    s3 = 100 / WM["xh"]; w3 = WM["w"] * s3
    out["wordmark"] = svg(int(w3 + 80), 220, f'<path transform="translate(40 {40 + 100 + 10 - WM["asc"] * s3 + 20}) scale({s3})" d="{WM["d"]}" fill="{ink}"/>', bg)
    return out

for name, (bg, ink, weight) in WAYS.items():
    d = os.path.join(ROOT, "assets", "logos", name); os.makedirs(d, exist_ok=True)
    for kind, text in lockups(bg, ink, weight).items():
        open(os.path.join(d, f"{kind}.svg"), "w").write(text)

# transparent versions for placing on any background
for name, (ink, weight) in {"transparent-dark-ink": ("#1D4A3A", "#B8892E"), "transparent-light-ink": ("#EDEAE1", "#E8C46A")}.items():
    d = os.path.join(ROOT, "assets", "logos", name); os.makedirs(d, exist_ok=True)
    for kind, text in lockups(None, ink, weight).items():
        open(os.path.join(d, f"{kind}.svg"), "w").write(text)
print("ok")
