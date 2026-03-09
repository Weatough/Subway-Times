#!/usr/bin/env python3
"""
Q Train Arrival Display for Unicorn HAT Mini
Parkside Avenue – uptown Q trains only.

Runs fully headless — all output goes to a rotating log file at
/var/log/q-train/q_train.log (or ~/q_train.log as fallback).
No terminal required.

Requirements:
    pip3 install requests protobuf gtfs-realtime-bindings unicornhatmini pillow
MTA API key:
    Set in the systemd service file or export MTA_API_KEY="your_key_here"
"""

import os
import sys
import time
import logging
import datetime
import requests
from logging.handlers import RotatingFileHandler
from PIL import Image, ImageDraw, ImageFont
from google.transit import gtfs_realtime_pb2

# ── Logging (file only — no terminal output) ──────────────────────────────────
def _setup_logging():
    log_dir = "/var/log/q-train"
    log_path = os.path.join(log_dir, "q_train.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        # Quick write test
        with open(log_path, "a"):
            pass
    except PermissionError:
        # Fall back to home directory if /var/log isn't writable
        log_path = os.path.expanduser("~/q_train.log")

    handler = RotatingFileHandler(
        log_path,
        maxBytes=1 * 1024 * 1024,   # 1 MB per file
        backupCount=3,               # keep 3 rotated files
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger = logging.getLogger("q_train")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger

log = _setup_logging()

# ── HAT ──────────────────────────────────────────────────────────────────────
try:
    from unicornhatmini import UnicornHATMini
    HAT_AVAILABLE = True
except ImportError:
    log.warning("unicornhatmini not found – display disabled, logging only")
    HAT_AVAILABLE = False

# ── MTA config ───────────────────────────────────────────────────────────────
MTA_API_KEY  = os.environ.get("MTA_API_KEY", "")
FEED_URL     = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-nqrw"
STOP_ID      = "Q05N"
ROUTE_ID     = "Q"
REFRESH_SECS = 30
MAX_ARRIVALS = 3

# ── Display constants ─────────────────────────────────────────────────────────
DISPLAY_WIDTH  = 17
DISPLAY_HEIGHT = 7
BRIGHTNESS     = 0.3
FRAME_SECS     = 0.05    # ~20 fps render loop
SCROLL_SPEED   = 0.055   # seconds per column during a swipe

COLOR_GREEN  = (  0, 220,   0)
COLOR_YELLOW = (255, 220,   0)
COLOR_RED    = (255,   0,   0)
COLOR_NONE   = ( 80,  80,  80)

URGENT_MINS   = 7
LINGER_URGENT = 10.0

# ── Colour math ───────────────────────────────────────────────────────────────

def lerp(a, b, t):
    return a + (b - a) * t

def color_for_minutes(mins_float):
    m = float(mins_float)
    if m >= 8.0:
        return COLOR_GREEN
    elif m >= 6.0:
        t = (m - 6.0) / 2.0
        return (
            int(lerp(COLOR_YELLOW[0], COLOR_GREEN[0], t)),
            int(lerp(COLOR_YELLOW[1], COLOR_GREEN[1], t)),
            int(lerp(COLOR_YELLOW[2], COLOR_GREEN[2], t)),
        )
    elif m >= 1.0:
        t = (m - 1.0) / 5.0
        return (
            int(lerp(COLOR_RED[0], COLOR_YELLOW[0], t)),
            int(lerp(COLOR_RED[1], COLOR_YELLOW[1], t)),
            int(lerp(COLOR_RED[2], COLOR_YELLOW[2], t)),
        )
    else:
        return COLOR_RED

# ── Pillow font ───────────────────────────────────────────────────────────────
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
_FONT_SIZE = 9
_THRESHOLD = 80

def _load_font():
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, _FONT_SIZE)
                log.info("Font loaded: %s @ %dpt", os.path.basename(path), _FONT_SIZE)
                return f
            except Exception as e:
                log.warning("Could not load font %s: %s", path, e)
    log.warning("No TTF font found, using Pillow default")
    return ImageFont.load_default()

_FONT = _load_font()

# ── Glyph rendering ───────────────────────────────────────────────────────────
_GLYPH_CACHE: dict = {}

def _render_char(ch):
    try:
        bbox = _FONT.getbbox(ch)
    except AttributeError:
        w, h = _FONT.getsize(ch)
        bbox = (0, 0, w, h)
    char_w   = max(bbox[2] - bbox[0], 1)
    canvas_h = DISPLAY_HEIGHT + 10
    img  = Image.new("L", (char_w + 4, canvas_h), 0)
    draw = ImageDraw.Draw(img)
    draw.text((-bbox[0], 2), ch, font=_FONT, fill=255)
    first_row, last_row = canvas_h, 0
    for y in range(canvas_h):
        for x in range(img.width):
            if img.getpixel((x, y)) > _THRESHOLD:
                first_row = min(first_row, y)
                last_row  = max(last_row,  y)
    if first_row > last_row:
        return [0, 0]
    rows_used = min(last_row - first_row + 1, DISPLAY_HEIGHT)
    top_pad   = (DISPLAY_HEIGHT - rows_used) // 2
    cols = []
    for x in range(img.width):
        mask = 0
        for i in range(rows_used):
            if img.getpixel((x, first_row + i)) > _THRESHOLD:
                bit = DISPLAY_HEIGHT - 1 - (top_pad + i)
                if 0 <= bit < DISPLAY_HEIGHT:
                    mask |= (1 << bit)
        cols.append(mask)
    while cols and cols[0]  == 0: cols.pop(0)
    while cols and cols[-1] == 0: cols.pop()
    return cols if cols else [0, 0]

def _glyph(ch):
    if ch not in _GLYPH_CACHE:
        _GLYPH_CACHE[ch] = _render_char(ch)
    return _GLYPH_CACHE[ch]

for _ch in "0123456789- ":
    _glyph(_ch)

# ── Column helpers ────────────────────────────────────────────────────────────

def text_to_masks(text):
    masks = []
    for i, ch in enumerate(text):
        masks.extend(_glyph(ch))
        if i < len(text) - 1:
            masks.append(0)
    return masks

def _pad_masks(masks):
    if len(masks) >= DISPLAY_WIDTH:
        return masks[:DISPLAY_WIDTH]
    pad   = DISPLAY_WIDTH - len(masks)
    left  = pad // 2
    right = pad - left
    return [0] * left + masks + [0] * right

def render_frame(hat, masks, color):
    if not hat:
        return
    hat.clear()
    padded = _pad_masks(masks)
    for x, mask in enumerate(padded):
        for row in range(DISPLAY_HEIGHT):
            if mask & (1 << (DISPLAY_HEIGHT - 1 - row)):
                hat.set_pixel(x, row, *color)
    hat.show()

# ── Swipe transition ──────────────────────────────────────────────────────────

def swipe(hat, from_masks, from_color, to_masks, to_color, direction="left"):
    if not hat:
        return
    fp = _pad_masks(from_masks)
    tp = _pad_masks(to_masks)
    W  = DISPLAY_WIDTH
    for step in range(W + 1):
        hat.clear()
        for x in range(W):
            if direction == "left":
                from_x = x + step
                to_x   = x - (W - step)
            else:
                from_x = x - step
                to_x   = x + (W - step)
            mask = color = None
            if 0 <= from_x < W and fp[from_x]:
                mask, color = fp[from_x], from_color
            elif 0 <= to_x < W and tp[to_x]:
                mask, color = tp[to_x], to_color
            if mask:
                for row in range(DISPLAY_HEIGHT):
                    if mask & (1 << (DISPLAY_HEIGHT - 1 - row)):
                        hat.set_pixel(x, row, *color)
        hat.show()
        time.sleep(SCROLL_SPEED)

# ── MTA feed ──────────────────────────────────────────────────────────────────

def fetch_arrivals():
    headers = {"x-api-key": MTA_API_KEY}
    try:
        resp = requests.get(FEED_URL, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Feed fetch failed: %s", exc)
        return []

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    now_ts  = time.time()
    results = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        if tu.trip.route_id != ROUTE_ID:
            continue
        for stu in tu.stop_time_update:
            if stu.stop_id != STOP_ID:
                continue
            ts   = stu.arrival.time if stu.arrival.time else stu.departure.time
            mins = (ts - now_ts) / 60.0
            if mins >= 0:
                results.append((ts, mins))

    results.sort()
    return results[:MAX_ARRIVALS]

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    if not MTA_API_KEY:
        log.critical("MTA_API_KEY is not set. Get a free key at https://api.mta.info/")
        sys.exit(1)

    hat = None
    if HAT_AVAILABLE:
        hat = UnicornHATMini()
        hat.set_brightness(BRIGHTNESS)
        hat.clear()
        hat.show()

    log.info("Q Train display started — stop=%s refresh=%ds", STOP_ID, REFRESH_SECS)

    last_fetch   = 0.0
    arrivals     = []
    showing_next = False
    card_start   = 0.0
    last_min_val = None

    try:
        while True:
            now = time.time()

            # Refresh feed
            if now - last_fetch >= REFRESH_SECS:
                arrivals   = fetch_arrivals()
                last_fetch = now
                if arrivals:
                    strs = [f"{m:.1f}" for _, m in arrivals]
                    log.info("Arrivals: %s min", ", ".join(strs))
                else:
                    log.info("No arrivals found")

            # No data
            if not arrivals:
                render_frame(hat, text_to_masks("-"), COLOR_NONE)
                time.sleep(1.0)
                continue

            # Recompute live minutes from stored timestamps
            now  = time.time()
            live = [(ts, (ts - now) / 60.0) for ts, _ in arrivals]
            live = [(ts, m) for ts, m in live if m >= 0]
            if not live:
                arrivals = []
                continue

            nearest_ts, nearest_mins = live[0]
            has_next  = len(live) >= 2
            next_mins = live[1][1] if has_next else None
            is_urgent = nearest_mins <= URGENT_MINS

            # Snap back to nearest if urgency expired
            if not is_urgent and showing_next:
                showing_next = False
                card_start   = now

            # Urgent alternation
            if is_urgent and has_next:
                elapsed = now - card_start
                if elapsed >= LINGER_URGENT:
                    n_masks = text_to_masks(str(int(nearest_mins)))
                    x_masks = text_to_masks(str(int(next_mins)))
                    n_color = color_for_minutes(nearest_mins)
                    x_color = color_for_minutes(next_mins)
                    if showing_next:
                        log.info("Swipe right → nearest (%d min)", int(nearest_mins))
                        swipe(hat, x_masks, x_color, n_masks, n_color, direction="right")
                        showing_next = False
                    else:
                        log.info("Swipe left → next (%d min)", int(next_mins))
                        swipe(hat, n_masks, n_color, x_masks, x_color, direction="left")
                        showing_next = True
                    card_start = time.time()

            # Render current card
            display_mins = next_mins if (showing_next and next_mins is not None) else nearest_mins
            if not showing_next:
                showing_next = False

            color = color_for_minutes(display_mins)
            masks = text_to_masks(str(int(display_mins)))

            if int(display_mins) != last_min_val:
                last_min_val = int(display_mins)
                log.info("Display: %d min  color=rgb%s", int(display_mins), color)

            render_frame(hat, masks, color)
            time.sleep(FRAME_SECS)

    except Exception as exc:
        log.exception("Unhandled exception: %s", exc)
        if hat:
            hat.clear()
            hat.show()
        raise

if __name__ == "__main__":
    main()
