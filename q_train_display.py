#!/usr/bin/env python3
"""
Q Train Arrival Display for Unicorn HAT Mini
Fetches uptown Q train arrivals at Parkside Avenue (MTA GTFS-RT feed)
and displays each arrival time centred on the full 7-row display,
lingering for 4 seconds then scrolling to the next.

Glyphs are rendered at runtime using Pillow from Liberation Sans Bold,
giving clean, natural-looking digits across the full 7-pixel display height.

Requirements:
    pip3 install requests protobuf gtfs-realtime-bindings unicornhatmini pillow

MTA API Key:
    Register free at https://api.mta.info/ and set the env var:
        export MTA_API_KEY="your_key_here"
"""

import os
import sys
import time
import datetime
import requests
from PIL import Image, ImageDraw, ImageFont
from google.transit import gtfs_realtime_pb2

# ── Optional: fall back gracefully if not on a Pi with the HAT ──────────────
try:
    from unicornhatmini import UnicornHATMini
    HAT_AVAILABLE = True
except ImportError:
    print("unicornhatmini not found – running in console-only mode.")
    HAT_AVAILABLE = False

# ── MTA Config ───────────────────────────────────────────────────────────────
MTA_API_KEY  = os.environ.get("MTA_API_KEY", "")
FEED_URL     = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-nqrw"
STOP_ID      = "Q05N"   # Parkside Ave – uptown / Manhattan-bound platform
ROUTE_ID     = "Q"
REFRESH_SECS = 30
MAX_ARRIVALS = 3

# ── Display Config ───────────────────────────────────────────────────────────
DISPLAY_WIDTH  = 17
DISPLAY_HEIGHT = 7
BRIGHTNESS     = 0.3
SCROLL_SPEED   = 0.055  # seconds per column step while scrolling
LINGER_SECS    = 4.0    # seconds to hold each arrival time on screen

COLOR_RED    = (255,   0,   0)   # 1-5 min  -> red
COLOR_YELLOW = (255, 220,   0)   # 6-8 min  -> yellow
COLOR_GREEN  = (  0, 220,   0)   # 9+ min   -> green
COLOR_NONE   = ( 80,  80,  80)   # no data  -> grey

# ── Pillow font setup ────────────────────────────────────────────────────────
# Liberation Sans Bold at size 9 renders all digits cleanly at exactly
# 7 pixels tall, with good stroke weight and open counters.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
_FONT_SIZE  = 9
_THRESHOLD  = 80   # brightness threshold (0–255) for on/off pixel

def _load_font():
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, _FONT_SIZE)
                print(f"Font loaded: {path} @ {_FONT_SIZE}pt")
                return f
            except Exception:
                pass
    print("Warning: no TTF font found, falling back to Pillow default bitmap font.")
    return ImageFont.load_default()

_FONT = _load_font()

# ── Glyph rendering via Pillow ────────────────────────────────────────────────

def _render_char(ch):
    """
    Render a single character with Pillow and return a list of column bitmasks.
    One int per pixel column; 7 bits per int (bit 6 = top row, bit 0 = bottom).
    The glyph is vertically centred within the 7-row display height.
    """
    try:
        bbox = _FONT.getbbox(ch)
    except AttributeError:
        w, h = _FONT.getsize(ch)
        bbox = (0, 0, w, h)

    char_w    = max(bbox[2] - bbox[0], 1)
    canvas_h  = DISPLAY_HEIGHT + 10

    # Draw character onto a greyscale canvas with a small top margin
    img  = Image.new("L", (char_w + 4, canvas_h), 0)
    draw = ImageDraw.Draw(img)
    draw.text((-bbox[0], 2), ch, font=_FONT, fill=255)

    # Locate the tight vertical bounds of lit pixels
    first_row, last_row = canvas_h, 0
    for y in range(canvas_h):
        for x in range(img.width):
            if img.getpixel((x, y)) > _THRESHOLD:
                first_row = min(first_row, y)
                last_row  = max(last_row,  y)

    if first_row > last_row:
        return [0, 0]   # blank / space

    glyph_h   = last_row - first_row + 1
    rows_used = min(glyph_h, DISPLAY_HEIGHT)
    top_pad   = (DISPLAY_HEIGHT - rows_used) // 2   # centre vertically

    cols = []
    for x in range(img.width):
        mask = 0
        for i in range(rows_used):
            if img.getpixel((x, first_row + i)) > _THRESHOLD:
                bit = DISPLAY_HEIGHT - 1 - (top_pad + i)
                if 0 <= bit < DISPLAY_HEIGHT:
                    mask |= (1 << bit)
        cols.append(mask)

    # Remove blank edge columns
    while cols and cols[0]  == 0: cols.pop(0)
    while cols and cols[-1] == 0: cols.pop()
    return cols if cols else [0, 0]


# Pre-warm the glyph cache at import time (zero latency during display loop)
_GLYPH_CACHE: dict[str, list[int]] = {}

def _glyph(ch: str) -> list[int]:
    if ch not in _GLYPH_CACHE:
        _GLYPH_CACHE[ch] = _render_char(ch)
    return _GLYPH_CACHE[ch]

for _ch in "0123456789m- ":
    _glyph(_ch)


# ── MTA feed ─────────────────────────────────────────────────────────────────

def fetch_arrivals():
    """Return sorted list of minutes-until-arrival for uptown Q trains."""
    headers = {"x-api-key": MTA_API_KEY}
    try:
        resp = requests.get(FEED_URL, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[{_now()}] Feed fetch error: {exc}")
        return []

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    now_ts  = time.time()
    minutes = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        if tu.trip.route_id != ROUTE_ID:
            continue
        for stu in tu.stop_time_update:
            if stu.stop_id != STOP_ID:
                continue
            arrival_ts = stu.arrival.time if stu.arrival.time else stu.departure.time
            mins = (arrival_ts - now_ts) / 60.0
            if mins >= 0:
                minutes.append(round(mins))

    minutes.sort()
    return minutes[:MAX_ARRIVALS]


def color_for_minutes(m):
    if m <= 5:  return COLOR_RED
    if m <= 8:  return COLOR_YELLOW
    return COLOR_GREEN


# ── Column strip builder ──────────────────────────────────────────────────────

def text_to_columns(text, color):
    """
    Convert a string to a list of (7-bit column mask, RGB color) tuples
    using Pillow-rendered glyphs. A 1-pixel gap is inserted between chars.
    """
    cols = []
    for i, ch in enumerate(text):
        for mask in _glyph(ch):
            cols.append((mask, color))
        if i < len(text) - 1:
            cols.append((0, color))   # 1-px inter-character gap
    return cols


# ── Rendering ────────────────────────────────────────────────────────────────

def render_columns(hat, col_data, offset):
    """Paint DISPLAY_WIDTH columns starting at offset onto the HAT."""
    hat.clear()
    for x in range(DISPLAY_WIDTH):
        idx = offset + x
        if idx < 0 or idx >= len(col_data):
            continue
        mask, color = col_data[idx]
        for row in range(DISPLAY_HEIGHT):
            if mask & (1 << (DISPLAY_HEIGHT - 1 - row)):
                hat.set_pixel(x, row, *color)
    hat.show()


def centre_offset(col_data):
    """Return the scroll offset that centres col_data on the display."""
    return max(0, (len(col_data) - DISPLAY_WIDTH) // 2)


def console_preview(col_data, label=""):
    """ASCII art preview printed to the terminal for debugging."""
    w = len(col_data)
    print(f"  [{label}]  ({w} columns wide)")
    print("  +" + "-" * w + "+")
    for row in range(DISPLAY_HEIGHT):
        line = "  |"
        for mask, _ in col_data:
            line += "#" if mask & (1 << (DISPLAY_HEIGHT - 1 - row)) else " "
        line += "|"
        print(line)
    print("  +" + "-" * w + "+\n")


def linger(hat, col_data, offset, seconds):
    """Hold a frame steady for the given number of seconds."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if hat:
            render_columns(hat, col_data, offset)
        time.sleep(SCROLL_SPEED)


def scroll_to_next(hat, from_cols, to_cols):
    """
    Scroll horizontally from one centred label to the next.
    Builds [from_cols][blank gap][to_cols] and scrolls from the centre
    of from_cols to the centre of to_cols.
    """
    gap      = [(0, COLOR_NONE)] * DISPLAY_WIDTH
    combined = from_cols + gap + to_cols

    start   = centre_offset(from_cols)
    to_base = len(from_cols) + len(gap)
    end     = to_base + centre_offset(to_cols)

    for offset in range(start, end + 1):
        if hat:
            render_columns(hat, combined, offset)
        time.sleep(SCROLL_SPEED)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def console_display(arrivals):
    if not arrivals:
        print(f"[{_now()}] No upcoming Q trains found.")
    else:
        parts = [f"{m} min" for m in arrivals]
        print(f"[{_now()}] Uptown Q @ Parkside Ave: {' | '.join(parts)}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    if not MTA_API_KEY:
        print("ERROR: Set MTA_API_KEY environment variable before running.")
        print("  Get a free key at https://api.mta.info/")
        sys.exit(1)

    hat = None
    if HAT_AVAILABLE:
        hat = UnicornHATMini()
        hat.set_brightness(BRIGHTNESS)
        hat.clear()
        hat.show()

    print(f"Q Train display running. Stop: {STOP_ID}  Refresh: {REFRESH_SECS}s")
    print("Press Ctrl-C to quit.\n")

    last_fetch = 0
    arrivals   = []

    try:
        while True:
            # Refresh arrivals from MTA if due
            if time.time() - last_fetch >= REFRESH_SECS:
                arrivals   = fetch_arrivals()
                last_fetch = time.time()
                console_display(arrivals)

            if not arrivals:
                cols   = text_to_columns("-", COLOR_NONE)
                offset = centre_offset(cols)
                if hat:
                    render_columns(hat, cols, offset)
                time.sleep(1.0)
                continue

            # Build column data for each arrival label (e.g. "2m", "9m", "14m")
            labels = []
            for m in arrivals:
                color = color_for_minutes(m)
                cols  = text_to_columns(f"{m}m", color)
                labels.append((cols, color, m))

            # Cycle: linger → scroll → linger → scroll → ...
            for i, (cols, color, m) in enumerate(labels):
                offset = centre_offset(cols)
                console_preview(cols, label=f"{m}m")

                # Hold this arrival on screen for LINGER_SECS
                linger(hat, cols, offset, LINGER_SECS)

                # Check for a fresh fetch before scrolling
                if time.time() - last_fetch >= REFRESH_SECS:
                    break

                # Scroll to next label (wraps to first after last)
                next_cols, _, _ = labels[(i + 1) % len(labels)]
                scroll_to_next(hat, cols, next_cols)

    except KeyboardInterrupt:
        print("\nExiting.")
        if hat:
            hat.clear()
            hat.show()


if __name__ == "__main__":
    main()
