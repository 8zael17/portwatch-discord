"""
PortWatch VI -> Discord (version GitHub Actions + webhook, 100% gratis)

Lee https://portwatch-vi.xyz/data/tracker.json, calcula el progreso de cada
proyecto, genera una imagen y la publica en un canal por webhook. La primera vez
crea el mensaje; las siguientes veces EDITA ese mismo mensaje (no hace spam).

NOTA: el sitio calcula sus porcentajes con su propio codigo. Aqui se usa una
ESTIMACION basada en su metodologia publica, por eso la imagen dice "estimado".
"""
import datetime
import io
import json
import math
import os
import random
import sys

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DATA_URL = "https://portwatch-vi.xyz/data/tracker.json"
SITE_URL = "https://portwatch-vi.xyz"
STATE_FILE = "state.json"

STEPS = ("loads", "boots", "menu", "world", "playable")
STEP_LABEL = {
    "loads": "Carga",
    "boots": "Arranca",
    "menu": "Menú",
    "world": "Mundo",
    "playable": "Jugable",
}


# ------------------------------------------------------------------- puntaje
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _clamp(v):
    return max(0.0, min(1.0, _num(v)))


def score_effort(e):
    """Estimacion 0-100: trabajo previo (10) + escalera de 5 pasos (40) + 4 medidas de calidad (50)."""
    g = e.get("gta") or {}

    gw = e.get("groundwork")
    if isinstance(gw, dict):
        vals = [_clamp(v) for v in gw.values()]
    elif isinstance(gw, (list, tuple)):
        vals = [_clamp(v) for v in gw]
    else:
        vals = []
    if vals:
        pts = 10 * sum(vals) / len(vals)
    else:
        pts = 10 if _num(g.get("loads")) > 0 else 0

    reached_playable = True
    for s in STEPS:
        v = _clamp(g.get(s))
        pts += 8 * v
        if v < 1:
            reached_playable = False
            break  # cada paso exige el anterior

    if reached_playable:  # las 4 medidas solo cuentan cuando ya es jugable
        fps = min(_num(g.get("fps")) / 30.0, 1.0)
        pts += 12.5 * (_clamp(g.get("story")) + _clamp(g.get("render")) + fps + _clamp(g.get("stability")))

    return min(pts, 100.0)


def stage_of(e):
    g = e.get("gta") or {}
    best = None
    for s in STEPS:
        if _num(g.get(s)) >= 1:
            best = s
        else:
            break
    return STEP_LABEL.get(best, "Sin arrancar")


def parse_tracker(data):
    efforts = data.get("efforts") or []
    tracked = [e for e in efforts if e.get("tier") == "tracked"] or efforts
    rows = []
    for e in tracked:
        name = str(e.get("name") or e.get("id") or "?")
        rows.append(
            {
                "name": name,
                "pct": score_effort(e),
                "stage": stage_of(e),
                "selfreported": e.get("verification") == "self-reported",
            }
        )
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows


# -------------------------------------------------------------------- imagen
def _font(size, bold=True):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def _lerp(a, b, t):
    return a + (b - a) * t


def _mix(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(_lerp(c1[i], c2[i], t)) for i in range(3))


def _multi(stops, t):
    t = max(stops[0][0], min(stops[-1][0], t))
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if t <= p1:
            return _mix(c0, c1, (t - p0) / (p1 - p0) if p1 > p0 else 0)
    return stops[-1][1]


def _glow(img, layer, radius, times=1):
    g = layer.filter(ImageFilter.GaussianBlur(radius))
    for _ in range(times):
        img.alpha_composite(g)


def _layer(img):
    return Image.new("RGBA", img.size, (0, 0, 0, 0))


def _text_w(d, text, font):
    return d.textlength(text, font=font)


def neon_text(img, x, y, text, font, fill, glow_color, stroke=0, stroke_fill=None, glow_r=9):
    L = _layer(img)
    ImageDraw.Draw(L).text((x, y), text, font=font, fill=glow_color + (255,),
                           stroke_width=3, stroke_fill=glow_color + (255,))
    _glow(img, L, glow_r, 2)
    ImageDraw.Draw(img).text((x, y), text, font=font, fill=fill,
                             stroke_width=stroke, stroke_fill=stroke_fill)


def grad_bar(img, x, y, w, h, pct, c1, c2):
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(30, 12, 62), outline=(95, 55, 160), width=2)
    fw = int(w * max(0.0, min(pct, 100.0)) / 100)
    if fw <= 0:
        return
    fw = max(fw, h)
    g = Image.new("RGBA", (fw, h))
    gd = ImageDraw.Draw(g)
    for i in range(fw):
        gd.line([(i, 0), (i, h)], fill=_mix(c1, c2, i / max(fw - 1, 1)) + (255,))
    shine = Image.new("RGBA", (fw, h), (0, 0, 0, 0))
    ImageDraw.Draw(shine).rectangle((0, 0, fw, h // 3), fill=(255, 255, 255, 60))
    g = Image.alpha_composite(g, shine)
    mask = Image.new("L", (fw, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, fw - 1, h - 1), radius=h // 2, fill=255)
    L = _layer(img)
    L.paste(g, (x, y), mask)
    _glow(img, L, 8, 1)
    img.paste(g, (x, y), mask)


def palm(d, bx, by, h, lean, col):
    n = 28
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append((bx + lean * (t ** 2) * h * 0.5, by - t * h))
    for i in range(n):
        w = int(_lerp(15, 7, i / n))
        d.line([pts[i], pts[i + 1]], fill=col, width=w)
        d.ellipse((pts[i][0] - w / 2, pts[i][1] - w / 2, pts[i][0] + w / 2, pts[i][1] + w / 2), fill=col)
    tx, ty = pts[-1]
    L = h * 0.5
    for a in (172, 148, 122, 92, 62, 36, 12, 200, -20):
        rad = math.radians(a)
        prev = (tx, ty)
        for j in range(1, 15):
            t = j / 14
            x = tx + math.cos(rad) * L * t
            y = ty - math.sin(rad) * L * t * 0.7 + (L * 0.6) * t * t
            wd = max(2, int(_lerp(9, 2, t)))
            d.line([prev, (x, y)], fill=col, width=wd)
            prev = (x, y)
    d.ellipse((tx - 11, ty - 8, tx + 11, ty + 14), fill=col)


STAGE_COLOR = {
    "Sin arrancar": (130, 120, 160),
    "Carga": (0, 230, 255),
    "Arranca": (90, 255, 170),
    "Menú": (255, 230, 80),
    "Mundo": (255, 160, 60),
    "Jugable": (255, 70, 170),
}
PALETTES = [
    ((255, 60, 170), (255, 190, 60)),
    ((0, 230, 255), (150, 90, 255)),
    ((120, 255, 150), (0, 220, 255)),
    ((255, 230, 80), (255, 70, 170)),
    ((255, 150, 60), (230, 50, 200)),
]


def render_image(rows, top=5):
    rows = rows[:top]
    W = 1000
    HEAD, HORIZON = 340, 240
    ROW_H, GAP = 86, 12
    PANEL_H = 156
    H = HEAD + 18 + PANEL_H + 18 + len(rows) * (ROW_H + GAP) + 78
    img = Image.new("RGBA", (W, H), (14, 6, 32, 255))
    d = ImageDraw.Draw(img)

    # --- cielo
    sky = [(0, (22, 6, 58)), (0.5, (125, 22, 150)), (0.78, (255, 62, 145)), (1, (255, 160, 60))]
    for y in range(HORIZON):
        d.line([(0, y), (W, y)], fill=_multi(sky, y / HORIZON) + (255,))
    # estrellas
    rnd = random.Random(7)
    for _ in range(55):
        sx, sy = rnd.randint(0, W), rnd.randint(0, int(HORIZON * 0.5))
        r = rnd.choice((1, 1, 2))
        d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=(255, 255, 255, 255))

    # --- sol con rayas
    R, cx, cy = 118, W // 2, HORIZON - 52
    sun = Image.new("RGBA", (2 * R, 2 * R))
    sd = ImageDraw.Draw(sun)
    for y in range(2 * R):
        sd.line([(0, y), (2 * R, y)], fill=_mix((255, 240, 90), (255, 45, 150), y / (2 * R)) + (255,))
    for k in range(6):
        y0 = R + 6 + k * 15
        sd.rectangle((0, y0, 2 * R, y0 + 2 + k * 2.6), fill=(0, 0, 0, 0))
    m = Image.new("L", (4 * R, 4 * R), 0)
    ImageDraw.Draw(m).ellipse((0, 0, 4 * R - 1, 4 * R - 1), fill=255)
    m = m.resize((2 * R, 2 * R), Image.LANCZOS)
    alpha = Image.new("L", (2 * R, 2 * R), 0)
    alpha.paste(sun.split()[3], (0, 0))
    mask = ImageChops.multiply(m, sun.split()[3])
    L = _layer(img)
    L.paste(sun, (cx - R, cy - R), mask)
    _glow(img, L, 26, 2)
    img.paste(sun, (cx - R, cy - R), mask)

    # --- suelo con rejilla
    d = ImageDraw.Draw(img)
    for y in range(HORIZON, HEAD):
        d.line([(0, y), (W, y)], fill=_mix((70, 10, 100), (14, 6, 34), (y - HORIZON) / (HEAD - HORIZON)) + (255,))
    grid = _layer(img)
    gd = ImageDraw.Draw(grid)
    for i in range(1, 9):
        yy = HORIZON + (HEAD - HORIZON) * (i / 8) ** 1.8
        gd.line([(0, yy), (W, yy)], fill=(255, 60, 210, 255), width=2)
    for k in range(-14, 15):
        gd.line([(W / 2 + k * 9, HORIZON), (W / 2 + k * 120, HEAD)], fill=(0, 225, 255, 255), width=2)
    _glow(img, grid, 5, 1)
    img.alpha_composite(grid)
    hz = _layer(img)
    ImageDraw.Draw(hz).line([(0, HORIZON), (W, HORIZON)], fill=(255, 235, 120, 255), width=4)
    _glow(img, hz, 7, 2)
    img.alpha_composite(hz)

    # --- palmeras
    d = ImageDraw.Draw(img)
    pc = (12, 4, 30, 255)
    palm(d, 62, HEAD - 6, 250, 0.55, pc)
    palm(d, 128, HEAD - 6, 175, 0.4, pc)
    palm(d, W - 62, HEAD - 6, 250, -0.55, pc)
    palm(d, W - 128, HEAD - 6, 175, -0.4, pc)

    # --- cartel superior
    d = ImageDraw.Draw(img)
    tag = "PORTWATCH VI  ·  TRACKER EN VIVO"
    tf = _font(15)
    tw = _text_w(d, tag, tf)
    d.rounded_rectangle((W / 2 - tw / 2 - 18, 16, W / 2 + tw / 2 + 18, 46), radius=15, fill=(255, 60, 170, 255))
    d.text((W / 2 - tw / 2, 22), tag, font=tf, fill=(255, 255, 255))

    # --- titulo
    title = "¿CORRE GTA VI EN PC?"
    size = 56
    while _text_w(d, title, _font(size)) > W - 290 and size > 30:
        size -= 2
    f = _font(size)
    tw = _text_w(d, title, f)
    neon_text(img, W / 2 - tw / 2, 62, title, f, (255, 255, 255), (255, 40, 170),
              stroke=3, stroke_fill=(40, 6, 70), glow_r=11)
    d = ImageDraw.Draw(img)
    sub = "Seguimiento de ports de la comunidad · se actualiza cada 3 h"
    sf = _font(17, False)
    sw = _text_w(d, sub, sf)
    d.text((W / 2 - sw / 2, 62 + size + 14), sub, font=sf, fill=(255, 245, 225),
           stroke_width=2, stroke_fill=(60, 8, 80))

    # --- panel principal
    top_y = HEAD + 18
    overall = rows[0]["pct"] if rows else 0.0
    L = _layer(img)
    ImageDraw.Draw(L).rounded_rectangle((40, top_y, W - 40, top_y + PANEL_H), radius=22,
                                        outline=(255, 60, 170, 255), width=5)
    _glow(img, L, 9, 2)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, top_y, W - 40, top_y + PANEL_H), radius=22, fill=(24, 8, 54, 255),
                        outline=(255, 90, 190, 255), width=3)
    d.text((68, top_y + 18), "PROGRESO ESTIMADO · EL MÁS AVANZADO", font=_font(15), fill=(255, 225, 80))
    nm = rows[0]["name"][:20] if rows else "-"
    d.text((68, top_y + 42), nm, font=_font(32), fill=(255, 255, 255))
    pf = _font(76)
    ptxt = f"{overall:.1f}%"
    pw = _text_w(d, ptxt, pf)
    neon_text(img, W - 68 - pw, top_y + 8, ptxt, pf, (255, 255, 255), (0, 220, 255),
              stroke=2, stroke_fill=(10, 40, 90), glow_r=10)
    grad_bar(img, 68, top_y + 108, W - 136, 32, overall, (0, 225, 255), (255, 60, 170))

    # --- filas
    y = top_y + PANEL_H + 18
    for i, r in enumerate(rows):
        c1, c2 = PALETTES[i % len(PALETTES)]
        L = _layer(img)
        ImageDraw.Draw(L).rounded_rectangle((40, y, W - 40, y + ROW_H), radius=18, outline=c1 + (255,), width=3)
        _glow(img, L, 6, 1)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((40, y, W - 40, y + ROW_H), radius=18, fill=(22, 9, 50, 255), outline=c1 + (255,), width=2)
        # numero
        d.ellipse((58, y + 14, 106, y + 62), fill=c1 + (255,), outline=(255, 255, 255, 255), width=2)
        nf = _font(26)
        ns = str(i + 1)
        d.text((82 - _text_w(d, ns, nf) / 2, y + 22), ns, font=nf, fill=(20, 5, 40))
        # nombre
        mark = " *" if r["selfreported"] else ""
        d.text((124, y + 10), f"{r['name'][:26]}{mark}", font=_font(25), fill=(255, 255, 255))
        # porcentaje y etapa
        pf2 = _font(30)
        pt = f"{r['pct']:.1f}%"
        pw2 = _text_w(d, pt, pf2)
        d.text((W - 62 - pw2, y + 6), pt, font=pf2, fill=c2)
        cf = _font(15)
        stg = r["stage"].upper()
        cw = _text_w(d, stg, cf) + 24
        cxr = W - 62 - pw2 - 16
        d.rounded_rectangle((cxr - cw, y + 12, cxr, y + 38), radius=13, fill=STAGE_COLOR.get(r["stage"], (200, 200, 200)))
        d.text((cxr - cw + 12, y + 17), stg, font=cf, fill=(25, 6, 45))
        grad_bar(img, 124, y + 54, W - 124 - 62, 20, r["pct"], c1, c2)
        y += ROW_H + GAP

    # --- pie
    d = ImageDraw.Draw(img)
    for x in range(W):
        d.line([(x, H - 8), (x, H)], fill=_mix((255, 60, 170), (0, 225, 255), x / W) + (255,))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    d.text((40, H - 66), "* dato auto-reportado, sin verificar  ·  puntajes estimados; los oficiales están en la web",
           font=_font(14, False), fill=(235, 215, 255))
    d.text((40, H - 42), f"portwatch-vi.xyz   |   Actualizado: {now}", font=_font(15), fill=(255, 190, 235))

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


# ------------------------------------------------------------------- discord
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send(png, text, message_id):
    """Edita el mensaje existente; si no existe (o fue borrado) crea uno nuevo."""
    payload = {"content": text, "attachments": [{"id": 0, "filename": "progreso.png"}]}
    if message_id:
        r = requests.patch(
            f"{WEBHOOK}/messages/{message_id}",
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": ("progreso.png", png, "image/png")},
            timeout=30,
        )
        if r.status_code == 404:
            message_id = None
        else:
            r.raise_for_status()
            return message_id

    r = requests.post(
        f"{WEBHOOK}?wait=true",
        data={"payload_json": json.dumps(payload)},
        files={"files[0]": ("progreso.png", png, "image/png")},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def main():
    if not WEBHOOK:
        sys.exit("Falta el secreto DISCORD_WEBHOOK_URL")

    data = requests.get(DATA_URL, timeout=20).json()
    rows = parse_tracker(data)
    if not rows:
        sys.exit("No encontré proyectos en el JSON.")

    png = render_image(rows)
    text = f"Se actualiza solo cada 3 horas. Puntajes estimados. Fuente: <{SITE_URL}>"

    state = load_state()
    new_id = send(png, text, state.get("message_id"))
    if new_id != state.get("message_id"):
        save_state({"message_id": new_id})
    print("OK, mensaje:", new_id)


if __name__ == "__main__":
    main()
