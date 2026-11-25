"""
PortWatch VI -> Discord (version GitHub Actions + webhook, 100% gratis)

Lee https://portwatch-vi.xyz/data/tracker.json, genera una imagen con el progreso
y la publica en un canal por webhook. La primera vez crea el mensaje; las
siguientes veces EDITA ese mismo mensaje (no hace spam).
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

# ---------------------------------------------------------------- lectura JSON
NAME_KEYS = ("name", "title", "project", "repo", "id")
PROGRESS_KEYS = ("progress", "score", "percent", "pct", "total")
LIST_KEYS = ("efforts", "projects", "race", "leaderboard", "entries")


def _find_list(node):
    if isinstance(node, list):
        return node if node and isinstance(node[0], dict) else None
    if isinstance(node, dict):
        for k in LIST_KEYS:
            if k in node:
                found = _find_list(node[k])
                if found:
                    return found
        for v in node.values():
            found = _find_list(v)
            if found:
                return found
    return None


def _pick(d, keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _to_percent(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v * 100 if v <= 1 else v


def parse_tracker(data):
    efforts = []
    for item in _find_list(data) or []:
        name = _pick(item, NAME_KEYS)
        pct = _to_percent(_pick(item, PROGRESS_KEYS))
        if name is not None and pct is not None:
            efforts.append((str(name), pct))
    efforts.sort(key=lambda e: e[1], reverse=True)

    overall = None
    if isinstance(data, dict):
        overall = _to_percent(_pick(data, ("overall", "overallProgress", "progress")))
    if overall is None and efforts:
        overall = efforts[0][1]
    return overall or 0.0, efforts


# ---------------------------------------------------------------------- imagen
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


def render_image(overall, efforts, top=5):
    efforts = efforts[:top]
    W, row = 900, 74
    H = 230 + row * len(efforts)
    img = Image.new("RGB", (W, H), (18, 18, 26))
    d = ImageDraw.Draw(img)

    d.text((40, 28), "¿GTA VI corre en PC ya?", font=_font(34, True), fill=(255, 255, 255))
    d.text((40, 74), "Seguimiento de portabilidad - PortWatch VI", font=_font(18), fill=(160, 160, 180))
    d.text((40, 116), "Progreso del esfuerzo más avanzado", font=_font(18), fill=(200, 200, 215))
    d.text((W - 130, 114), f"{overall:.1f}%", font=_font(22, True), fill=(255, 255, 255))
    _bar(d, 40, 146, W - 80, 30, overall, (255, 94, 160))

    y = 200
    for i, (name, pct) in enumerate(efforts, 1):
        d.text((40, y), f"{i}. {name[:42]}", font=_font(18, True), fill=(235, 235, 245))
        d.text((W - 110, y), f"{pct:.1f}%", font=_font(18), fill=(200, 200, 215))
        _bar(d, 40, y + 30, W - 80, 16, pct, (80, 200, 255))
        y += row

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    d.text((40, H - 34), f"Datos: portwatch-vi.xyz  |  Actualizado: {now}", font=_font(14), fill=(120, 120, 140))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------- discord
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
    if message_id:
        payload = {"content": text, "attachments": [{"id": 0, "filename": "progreso.png"}]}
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

    payload = {"content": text, "attachments": [{"id": 0, "filename": "progreso.png"}]}
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
    overall, efforts = parse_tracker(data)
    if not efforts:
        sys.exit("No pude leer los proyectos del JSON: hay que ajustar parse_tracker().")

    png = render_image(overall, efforts)
    text = f"Se actualiza solo cada 3 horas. Fuente: <{SITE_URL}>"

    state = load_state()
    new_id = send(png, text, state.get("message_id"))
    if new_id != state.get("message_id"):
        save_state({"message_id": new_id})
    print("OK, mensaje:", new_id)


if __name__ == "__main__":
    main()
