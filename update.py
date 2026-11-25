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
import os
import sys

import requests
from PIL import Image, ImageDraw, ImageFont

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
def _font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def _bar(d, x, y, w, h, pct, color):
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(40, 42, 54))
    fill_w = int(w * max(0, min(pct, 100)) / 100)
    if fill_w > 0:
        d.rounded_rectangle((x, y, x + max(fill_w, h), y + h), radius=h // 2, fill=color)


def render_image(rows, top=5):
    rows = rows[:top]
    overall = rows[0]["pct"] if rows else 0.0
    W, row_h = 900, 74
    H = 240 + row_h * len(rows)
    img = Image.new("RGB", (W, H), (18, 18, 26))
    d = ImageDraw.Draw(img)

    d.text((40, 28), "¿GTA VI corre en PC ya?", font=_font(34, True), fill=(255, 255, 255))
    d.text((40, 74), "Seguimiento de portabilidad - PortWatch VI", font=_font(18), fill=(160, 160, 180))
    d.text((40, 116), "Progreso del esfuerzo más avanzado (estimado)", font=_font(18), fill=(200, 200, 215))
    d.text((W - 130, 114), f"{overall:.1f}%", font=_font(22, True), fill=(255, 255, 255))
    _bar(d, 40, 146, W - 80, 30, overall, (255, 94, 160))

    y = 200
    for i, r in enumerate(rows, 1):
        mark = " *" if r["selfreported"] else ""
        d.text((40, y), f"{i}. {r['name'][:34]}{mark}", font=_font(18, True), fill=(235, 235, 245))
        right = f"{r['stage']}  ·  {r['pct']:.1f}%"
        tw = d.textlength(right, font=_font(18))
        d.text((W - 40 - tw, y), right, font=_font(18), fill=(200, 200, 215))
        _bar(d, 40, y + 30, W - 80, 16, r["pct"], (80, 200, 255))
        y += row_h

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    d.text((40, H - 54), "* dato auto-reportado, sin verificar.  Puntajes estimados; los oficiales están en la web.",
           font=_font(13), fill=(140, 140, 160))
    d.text((40, H - 32), f"Datos: portwatch-vi.xyz  |  Actualizado: {now}", font=_font(14), fill=(120, 120, 140))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


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
