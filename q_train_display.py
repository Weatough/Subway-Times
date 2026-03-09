#!/usr/bin/env python3
"""
Q Train Arrival Display for Unicorn HAT Mini
Fetches uptown Q train arrivals at Parkside Avenue (MTA GTFS-RT feed)
and scrolls the minutes-until-arrival across the display.

Requirements:
    pip3 install requests protobuf gtfs-realtime-bindings unicornhatmini

MTA API Key:
    Register free at https://api.mta.info/ and set the env var:
        export MTA_API_KEY="your_key_here"
"""

import os
import sys
import time
import math
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
MTA_API_KEY   = os.environ.get("MTA_API_KEY", "")
# GTFS-RT feed for B/D/F/M/N/Q/R/W lines
FEED_URL      = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-nqrw"
# GTFS stop IDs: Parkside Avenue uptown (Manhattan-bound) = Q05N
STOP_ID       = "Q05N"
ROUTE_ID      = "Q"
REFRESH_SECS  = 30   # how often to re-fetch from the MTA
MAX_ARRIVALS  = 3    # how many upcoming trains to show

# ── Display Config ───────────────────────────────────────────────────────────
BRIGHTNESS    = 0.3  # 0.0 – 1.0
SCROLL_SPEED  = 0.07 # seconds per column shift while scrolling
# Colour per minute bucket  (R, G, B)
COLOR_NOW     = (255,   0,   0)   # ≤ 1 min  → red (boarding!)
COLOR_SOON    = (255, 140,   0)   # 2–4 min  → orange
COLOR_OK      = (  0, 255,   0)   # 5–9 min  → green
COLOR_FAR     = (  0, 100, 255)   # ≥10 min  → blue

# ── Tiny 4-row pixel font (digits 0-9, colon, space, 'm') ───────────────────
# Each glyph is a list of column bit-masks, MSB = top row.
FONT = {
    '0': [0b1110, 0b1010, 0b1010, 0b1110],
    '1': [0b0100, 0b1100, 0b0100, 0b1110],
    '2': [0b1110, 0b0010, 0b0100, 0b1110],
    '3': [0b1110, 0b0110, 0b0010, 0b1110],
    '4': [0b1010, 0b1110, 0b0010, 0b0010],
    '5': [0b1110, 0b1100, 0b0010, 0b1110],
    '6': [0b1110, 0b1100, 0b1010, 0b1110],
    '7': [0b1110, 0b0010, 0b0100, 0b0100],
    '8': [0b1110, 0b1110, 0b1010, 0b1110],
    '9': [0b1110, 0b1010, 0b0110, 0b0010],
    'm': [0b0000, 0b1010, 0b1010, 0b1010],
    ' ': [0b0000, 0b0000, 0b0000, 0b0000],
    '-': [0b0000, 0b1110, 0b0000, 0b0000],
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
    if m <= 1:   return COLOR_NOW
    if m <= 4:   return COLOR_SOON
    if m <= 9:   return COLOR_OK
    return COLOR_FAR


def build_pixel_columns(arrivals):
    """Convert a list of minute values into a list of (col_pixels, color)."""
    # Build text like "2m 7m 14m" with colours per segment
    segments = []
    for i, m in enumerate(arrivals):
        label = f"{m}m"
        if i:
            label = " " + label
        segments.append((label, color_for_minutes(m)))

    if not segments:
        segments = [("-", (80, 80, 80))]

    # Expand into (4-bit column mask, color) pairs
    col_data = []  # list of (mask_int, color_tuple)
    for text, color in segments:
        for ch in text:
            glyph = FONT.get(ch, FONT[' '])
            for col_mask in glyph:
                col_data.append((col_mask, color))
            col_data.append((0b0000, color))   # 1-pixel gap between chars

    return col_data


def render_column(hat, display_cols, col_data, offset):
    """Write one frame: 17 display columns starting at offset in col_data."""
    hat.clear()
    width = 17  # UnicornHATMini width
    height = 7  # UnicornHATMini height

    # Centre the 4-row font vertically (rows 1-4 of the 7-row display)
    row_offset = 1

    for x in range(width):
        data_idx = offset + x
        if data_idx >= len(col_data):
            break
        mask, color = col_data[data_idx]
        for bit in range(4):
            if mask & (1 << (3 - bit)):
                y = row_offset + bit
                if y < height:
                    hat.set_pixel(x, y, *color)

    hat.show()


def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def console_display(arrivals):
    if not arrivals:
        print(f"[{_now()}] No upcoming Q trains found.")
        return
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

    arrivals       = []
    last_fetch     = 0
    col_data       = []
    scroll_offset  = 0

    print(f"Q Train display running. Stop: {STOP_ID}  Refresh: {REFRESH_SECS}s")
    print("Press Ctrl-C to quit.\n")

    try:
        while True:
            now = time.time()

            # Re-fetch from MTA every REFRESH_SECS seconds
            if now - last_fetch >= REFRESH_SECS:
                arrivals   = fetch_arrivals()
                last_fetch = now
                col_data   = build_pixel_columns(arrivals)
                scroll_offset = 0
                console_display(arrivals)

            if hat and col_data:
                render_column(hat, 17, col_data, scroll_offset)
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
