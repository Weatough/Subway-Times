#!/usr/bin/env python3
"""
Q Train Arrival Display for Unicorn HAT Mini
Fetches uptown Q train arrivals at Parkside Avenue (MTA GTFS-RT feed)
and scrolls the minutes-until-arrival across the full 7-row display.

Requirements:
    pip3 install requests protobuf gtfs-realtime-bindings unicornhatmini

MTA API Key:
    Register free at https://api.mta.info/ and set the env var:
        export MTA_API_KEY="your_key_here"
"""

import os
import sys
import time
import datetime
import requests
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
SCROLL_SPEED   = 0.07   # seconds per column step

COLOR_NOW  = (255,   0,   0)   # ≤ 1 min  → red   (board now!)
COLOR_SOON = (255, 140,   0)   # 2–4 min  → orange
COLOR_OK   = (  0, 255,   0)   # 5–9 min  → green
COLOR_FAR  = (  0, 100, 255)   # ≥10 min  → blue
COLOR_NONE = ( 80,  80,  80)   # no data  → grey

# ── 7-row pixel font ─────────────────────────────────────────────────────────
# Each glyph is a list of column integers, one per column of the character.
# Bit 6 (MSB of 7 bits) = top row, bit 0 = bottom row.
# Digits are 4 columns wide; 'm' is 5; space is 3; '-' is 4.

FONT7 = {
    '0': [
        0b1111110,
        0b1000010,
        0b1000010,
        0b1111110,
    ],
    '1': [
        0b0000100,
        0b1111110,
        0b0000000,
        0b0000000,
    ],
    '2': [
        0b1000110,
        0b1001010,
        0b1001010,
        0b1110010,
    ],
    '3': [
        0b1000010,
        0b1001010,
        0b1001010,
        0b1111110,
    ],
    '4': [
        0b0111000,
        0b0001000,
        0b0001000,
        0b1111110,
    ],
    '5': [
        0b1110010,
        0b1001010,
        0b1001010,
        0b1001110,
    ],
    '6': [
        0b1111110,
        0b1001010,
        0b1001010,
        0b1001110,
    ],
    '7': [
        0b1000000,
        0b1000110,
        0b1111000,
        0b0000000,
    ],
    '8': [
        0b1111110,
        0b1001010,
        0b1001010,
        0b1111110,
    ],
    '9': [
        0b1110010,
        0b1001010,
        0b1001010,
        0b1111110,
    ],
    'm': [
        0b0111110,
        0b0100000,
        0b0011110,
        0b0100000,
        0b0111110,
    ],
    ' ': [
        0b0000000,
        0b0000000,
        0b0000000,
    ],
    '-': [
        0b0001000,
        0b0001000,
        0b0001000,
        0b0001000,
    ],
}


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
    if m <= 1:  return COLOR_NOW
    if m <= 4:  return COLOR_SOON
    if m <= 9:  return COLOR_OK
    return COLOR_FAR


def build_pixel_columns(arrivals):
    """
    Build a list of (7-bit column mask, RGB color) tuples representing
    the full scrollable message at 7 pixels tall.
    """
    segments = []
    for i, m in enumerate(arrivals):
        label = f"{m}m"
        if i:
            label = " " + label
        segments.append((label, color_for_minutes(m)))

    if not segments:
        segments = [("-", COLOR_NONE)]

    col_data = []
    for text, color in segments:
        for ch in text:
            glyph = FONT7.get(ch, FONT7[' '])
            for col_mask in glyph:
                col_data.append((col_mask, color))
            col_data.append((0b0000000, color))  # 1-pixel gap between chars

    return col_data


def render_frame(hat, col_data, offset):
    """Render one scrolled frame onto the HAT Mini."""
    hat.clear()
    for x in range(DISPLAY_WIDTH):
        data_idx = offset + x
        if data_idx >= len(col_data):
            break
        mask, color = col_data[data_idx]
        for row in range(DISPLAY_HEIGHT):
            # bit 6 → row 0 (top), bit 0 → row 6 (bottom)
            if mask & (1 << (DISPLAY_HEIGHT - 1 - row)):
                hat.set_pixel(x, row, *color)
    hat.show()


def console_preview(col_data):
    """Print an ASCII preview of the pixel data to the terminal."""
    print("┌" + "─" * len(col_data) + "┐")
    for row in range(DISPLAY_HEIGHT):
        line = "│"
        for mask, _ in col_data:
            line += "█" if mask & (1 << (DISPLAY_HEIGHT - 1 - row)) else " "
        line += "│"
        print(line)
    print("└" + "─" * len(col_data) + "┘")


def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def console_display(arrivals):
    if not arrivals:
        print(f"[{_now()}] No upcoming Q trains found.")
    else:
        parts = [f"{m} min" for m in arrivals]
        print(f"[{_now()}] Uptown Q @ Parkside Ave: {' | '.join(parts)}")


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

    arrivals      = []
    last_fetch    = 0
    col_data      = []
    scroll_offset = 0

    print(f"Q Train display running. Stop: {STOP_ID}  Refresh: {REFRESH_SECS}s")
    print("Press Ctrl-C to quit.\n")

    try:
        while True:
            now = time.time()

            # Re-fetch from MTA every REFRESH_SECS seconds
            if now - last_fetch >= REFRESH_SECS:
                arrivals      = fetch_arrivals()
                last_fetch    = now
                col_data      = build_pixel_columns(arrivals)
                scroll_offset = 0
                console_display(arrivals)
                console_preview(col_data)

            if hat and col_data:
                render_frame(hat, col_data, scroll_offset)
                scroll_offset += 1
                if scroll_offset >= len(col_data):
                    scroll_offset = 0   # loop the scroll

            time.sleep(SCROLL_SPEED)

    except KeyboardInterrupt:
        print("\nExiting.")
        if hat:
            hat.clear()
            hat.show()


if __name__ == "__main__":
    main()
