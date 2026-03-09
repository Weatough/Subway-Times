#!/usr/bin/env python3
"""
Q Train Arrival Display for Unicorn HAT Mini
Parkside Avenue – uptown Q trains only.

Behaviour
─────────
• Normally shows only the nearest train, colour-coded by urgency.
• Colour transitions smoothly:
    ≥ 8 min  → pure green   (0, 220, 0)
    6 min    → pure yellow  (255, 220, 0)   (green→yellow between 8→6)
    1 min    → pure red     (255, 0, 0)     (yellow→red between 6→1)
• When nearest train is ≤ 7 min away:
    – Show nearest for 10 s, swipe LEFT to next train
    – Show next train for 10 s, swipe RIGHT back to nearest
    – Repeat until nearest train departs (drops off feed)
• When nearest train is > 7 min away:
    – Show nearest indefinitely, updating colour each render tick.
• MTA feed is refreshed every 30 s.
• The displayed number updates whenever the minute value changes.

Requirements:
    pip3 install requests protobuf gtfs-realtime-bindings unicornhatmini pillow
MTA API key:
    export MTA_API_KEY="your_key_here"
"""

import os
import sys
import time
import datetime
import requests
from PIL import Image, ImageDraw, ImageFont
from google.transit import gtfs_realtime_pb2

# ── HAT ──────────────────────────────────────────────────────────────────────
try:
    from unicornhatmini import UnicornHATMini
    HAT_AVAILABLE = True
except ImportError:
    print("unicornhatmini not found – console-only mode.")
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
FRAME_SECS     = 0.05   # ~20 fps render loop
SCROLL_SPEED   = 0.055  # seconds per column during a swipe

# Colour gradient anchor points  (minutes → RGB)
# Interpolated smoothly between these.
COLOR_GREEN  = (  0, 220,   0)
COLOR_YELLOW = (255, 220,   0)
COLOR_RED    = (255,   0,   0)
COLOR_NONE   = ( 80,  80,  80)

# When nearest is ≤ this many minutes, alternate with next train
URGENT_MINS      = 7
LINGER_URGENT    = 10.0   # seconds on each card when urgent
LINGER_NORMAL    = None   # hold indefinitely (just keep refreshing colour)

# ── Colour math ───────────────────────────────────────────────────────────────

def lerp(a, b, t):
    """Linear interpolate between a and b by factor t (0.0–1.0)."""
    return a + (b - a) * t

def color_for_minutes(mins_float):
    """
    Smooth colour gradient:
      ≥ 8 min  → green
      6 min    → yellow   (green↔yellow between 8 and 6)
      1 min    → red      (yellow↔red  between 6 and 1)
      ≤ 1 min  → red
    """
    m = float(mins_float)
    if m >= 8.0:
        r, g, b = COLOR_GREEN
    elif m >= 6.0:
        t = (m - 6.0) / (8.0 - 6.0)   # 1.0 at 8 min, 0.0 at 6 min
        r = int(lerp(COLOR_YELLOW[0], COLOR_GREEN[0], t))
        g = int(lerp(COLOR_YELLOW[1], COLOR_GREEN[1], t))
        b = int(lerp(COLOR_YELLOW[2], COLOR_GREEN[2], t))
    elif m >= 1.0:
        t = (m - 1.0) / (6.0 - 1.0)   # 1.0 at 6 min, 0.0 at 1 min
        r = int(lerp(COLOR_RED[0], COLOR_YELLOW[0], t))
        g = int(lerp(COLOR_RED[1], COLOR_YELLOW[1], t))
        b = int(lerp(COLOR_RED[2], COLOR_YELLOW[2], t))
    else:
        r, g, b = COLOR_RED
    return (r, g, b)

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
                print(f"Font: {os.path.basename(path)} @ {_FONT_SIZE}pt")
                return f
            except Exception:
                pass
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
    """Return a plain list of 7-bit column masks (no colour) for a string."""
    masks = []
    for i, ch in enumerate(text):
        masks.extend(_glyph(ch))
        if i < len(text) - 1:
            masks.append(0)
    return masks

def _pad_masks(masks):
    """Pad a mask list to DISPLAY_WIDTH, centred."""
    if len(masks) >= DISPLAY_WIDTH:
        return masks[:DISPLAY_WIDTH]
    pad   = DISPLAY_WIDTH - len(masks)
    left  = pad // 2
    right = pad - left
    return [0] * left + masks + [0] * right

def render_frame(hat, masks, color):
    """Render a padded mask list onto the HAT in a single colour."""
    if not hat:
        return
    hat.clear()
    padded = _pad_masks(masks)
    for x, mask in enumerate(padded):
        for row in range(DISPLAY_HEIGHT):
            if mask & (1 << (DISPLAY_HEIGHT - 1 - row)):
                hat.set_pixel(x, row, *color)
    hat.show()

def console_preview(masks, color, label=""):
    """Print ASCII art of the centred glyph to stdout."""
    padded = _pad_masks(masks)
    r, g, b = color
    print(f"  [{label}]  color=({r},{g},{b})")
    print("  +" + "─" * DISPLAY_WIDTH + "+")
    for row in range(DISPLAY_HEIGHT):
        line = "  │"
        for mask in padded:
            line += "█" if mask & (1 << (DISPLAY_HEIGHT - 1 - row)) else " "
        line += "│"
        print(line)
    print("  +" + "─" * DISPLAY_WIDTH + "+")

# ── Swipe transition ──────────────────────────────────────────────────────────

def swipe(hat, from_masks, from_color, to_masks, to_color, direction="left"):
    """
    Animate a swipe between two centred glyphs.
    direction="left"  → next card slides in from the right
    direction="right" → next card slides in from the left
    """
    if not hat:
        return
    fp = _pad_masks(from_masks)
    tp = _pad_masks(to_masks)
    W  = DISPLAY_WIDTH

    for step in range(W + 1):
        hat.clear()
        for x in range(W):
            if direction == "left":
                # from slides out left, to slides in from right
                from_x = x + step
                to_x   = x - (W - step)
            else:
                # from slides out right, to slides in from left
                from_x = x - step
                to_x   = x + (W - step)

            mask  = None
            color = None
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
    """Return sorted list of (arrival_unix_timestamp, minutes_float) tuples."""
    headers = {"x-api-key": MTA_API_KEY}
    try:
        resp = requests.get(FEED_URL, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[{_now()}] fetch error: {exc}")
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

def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    if not MTA_API_KEY:
        print("ERROR: set MTA_API_KEY environment variable.")
        print("  Free key at https://api.mta.info/")
        sys.exit(1)

    hat = None
    if HAT_AVAILABLE:
        hat = UnicornHATMini()
        hat.set_brightness(BRIGHTNESS)
        hat.clear()
        hat.show()

    print(f"Q Train display — stop {STOP_ID}  refresh {REFRESH_SECS}s")
    print("Ctrl-C to quit.\n")

    last_fetch    = 0.0
    arrivals      = []          # list of (ts, mins_float)
    showing_next  = False       # True while the 2nd-train card is displayed
    card_start    = 0.0         # when current card began lingering
    last_min_val  = None        # track integer minute to detect digit change

    try:
        while True:
            now = time.time()

            # ── Refresh feed ─────────────────────────────────────────────────
            if now - last_fetch >= REFRESH_SECS:
                arrivals   = fetch_arrivals()
                last_fetch = now
                if arrivals:
                    strs = [f"{m:.1f}" for _, m in arrivals]
                    print(f"[{_now()}] arrivals: {', '.join(strs)} min")
                else:
                    print(f"[{_now()}] no arrivals found")

            # ── No data ──────────────────────────────────────────────────────
            if not arrivals:
                render_frame(hat, text_to_masks("-"), COLOR_NONE)
                time.sleep(1.0)
                continue

            # ── Compute live minutes from stored timestamps ───────────────────
            now         = time.time()
            live        = [(ts, (ts - now) / 60.0) for ts, _ in arrivals]
            live        = [(ts, m) for ts, m in live if m >= 0]
            if not live:
                arrivals = []
                continue

            nearest_ts, nearest_mins = live[0]
            has_next  = len(live) >= 2
            next_mins = live[1][1] if has_next else None

            nearest_int = int(nearest_mins)
            is_urgent   = nearest_mins <= URGENT_MINS

            # ── Decide which card to show ─────────────────────────────────────
            # If urgency just turned off, snap back to nearest
            if not is_urgent and showing_next:
                showing_next = False
                card_start   = now

            if is_urgent and has_next:
                # Alternate between nearest and next every LINGER_URGENT seconds
                elapsed = now - card_start
                if elapsed >= LINGER_URGENT:
                    # Time to swipe
                    if showing_next:
                        # swipe right back to nearest
                        n_masks = text_to_masks(str(int(nearest_mins)))
                        x_masks = text_to_masks(str(int(next_mins)))
                        n_color = color_for_minutes(nearest_mins)
                        x_color = color_for_minutes(next_mins)
                        console_preview(n_masks, n_color, label=f"← {int(nearest_mins)} (nearest)")
                        swipe(hat, x_masks, x_color, n_masks, n_color, direction="right")
                        showing_next = False
                    else:
                        # swipe left to next
                        n_masks = text_to_masks(str(int(nearest_mins)))
                        x_masks = text_to_masks(str(int(next_mins)))
                        n_color = color_for_minutes(nearest_mins)
                        x_color = color_for_minutes(next_mins)
                        console_preview(x_masks, x_color, label=f"→ {int(next_mins)} (next)")
                        swipe(hat, n_masks, n_color, x_masks, x_color, direction="left")
                        showing_next = True
                    card_start = time.time()

            # ── Render current card ───────────────────────────────────────────
            if showing_next and next_mins is not None:
                display_mins = next_mins
            else:
                display_mins = nearest_mins
                showing_next = False

            color  = color_for_minutes(display_mins)
            masks  = text_to_masks(str(int(display_mins)))

            # Print to console only when the integer minute changes
            if int(display_mins) != last_min_val:
                last_min_val = int(display_mins)
                console_preview(masks, color, label=f"{display_mins:.1f} min")

            render_frame(hat, masks, color)
            time.sleep(FRAME_SECS)

    except KeyboardInterrupt:
        print("\nExiting.")
        if hat:
            hat.clear()
            hat.show()

if __name__ == "__main__":
    main()
