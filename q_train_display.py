#!/usr/bin/env python3
"""
Q Train Arrival Display for Unicorn HAT Mini
Fetches uptown Q train arrivals at Parkside Avenue (MTA GTFS-RT feed)
and displays each arrival time centred on the full 7-row display,
lingering for 4 seconds then scrolling to the next.

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
SCROLL_SPEED   = 0.055  # seconds per column step while scrolling
LINGER_SECS    = 4.0    # seconds to hold each arrival time on screen

COLOR_NOW  = (255,   0,   0)   # <= 1 min  -> red   (board now!)
COLOR_SOON = (255, 140,   0)   # 2-4 min   -> orange
COLOR_OK   = (  0, 220,   0)   # 5-9 min   -> green
COLOR_FAR  = (  0, 100, 255)   # >=10 min  -> blue
COLOR_NONE = ( 80,  80,  80)   # no data   -> grey

# ── 7-tall x 4-wide pixel font ───────────────────────────────────────────────
# Each entry is a list of column integers (one int per pixel column).
# 7 bits per column: bit 6 (MSB) = top row, bit 0 = bottom row.
# Every digit glyph is exactly 4 columns wide for consistency.
# 'm' is 5 columns. ' ' gap is 2 columns. '-' is 4 columns.
#
#  Bit layout visualised (7 rows):
#    bit 6  ██████
#    bit 5  ██████
#    bit 4  ██████
#    bit 3  ██████
#    bit 2  ██████
#    bit 1  ██████
#    bit 0  ██████

FONT7 = {
    # 0:  ░███░
    #     █░░░█
    #     █░░░█
    #     █░░░█
    #     █░░░█
    #     █░░░█
    #     ░███░
    '0': [0b0111110,
          0b1000001,
          0b1000001,
          0b0111110],

    # 1:  ░░█░░
    #     ░██░░
    #     ░░█░░
    #     ░░█░░
    #     ░░█░░
    #     ░░█░░
    #     ░███░
    '1': [0b0000010,
          0b0000001,
          0b1111111,
          0b0000000],

    # 2:  ░███░
    #     █░░░█
    #     ░░░░█
    #     ░░██░
    #     ░█░░░
    #     █░░░░
    #     █████
    '2': [0b1000011,
          0b1000101,
          0b1001001,
          0b0110001],

    # 3:  ░███░
    #     █░░░█
    #     ░░░░█
    #     ░░██░
    #     ░░░░█
    #     █░░░█
    #     ░███░
    '3': [0b1000010,
          0b1001001,
          0b1001001,
          0b0110110],

    # 4:  ░░░█░
    #     ░░██░
    #     ░█░█░
    #     █░░█░
    #     █████
    #     ░░░█░
    #     ░░░█░
    '4': [0b0011100,
          0b0100100,
          0b1111111,
          0b0000100],

    # 5:  █████
    #     █░░░░
    #     █████
    #     ░░░░█
    #     ░░░░█
    #     █░░░█
    #     ░███░
    '5': [0b1111010,
          0b1001001,
          0b1001001,
          0b1000110],

    # 6:  ░███░
    #     █░░░░
    #     █████
    #     █░░░█
    #     █░░░█
    #     █░░░█
    #     ░███░
    '6': [0b0111110,
          0b1001001,
          0b1001001,
          0b0000110],

    # 7:  █████
    #     ░░░░█
    #     ░░░█░
    #     ░░█░░
    #     ░█░░░
    #     ░█░░░
    #     ░█░░░
    '7': [0b1000000,
          0b1000111,
          0b1111000,
          0b0000000],

    # 8:  ░███░
    #     █░░░█
    #     █░░░█
    #     ░███░
    #     █░░░█
    #     █░░░█
    #     ░███░
    '8': [0b0110110,
          0b1001001,
          0b1001001,
          0b0110110],

    # 9:  ░███░
    #     █░░░█
    #     █░░░█
    #     ░████
    #     ░░░░█
    #     ░░░░█
    #     ░███░
    '9': [0b1110000,
          0b1001001,
          0b1001001,
          0b0111110],

    # m:  ░░░░░
    #     ░░░░░
    #     ███░█
    #     █░█░█
    #     █░█░█
    #     █░█░█
    #     █░█░█
    'm': [0b0011111,
          0b0100000,
          0b0011100,
          0b0100000,
          0b0011111],

    ' ': [0b0000000,
          0b0000000],

    '-': [0b0001000,
          0b0001000,
          0b0001000,
          0b0001000],
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


def text_to_columns(text, color):
    """Convert a string to a list of (7-bit mask, color) column tuples."""
    cols = []
    for i, ch in enumerate(text):
        glyph = FONT7.get(ch, FONT7[' '])
        for mask in glyph:
            cols.append((mask, color))
        if i < len(text) - 1:
            cols.append((0b0000000, color))  # 1-px gap between chars
    return cols


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
    """ASCII art preview for terminal debugging."""
    print(f"  [{label}]")
    for row in range(DISPLAY_HEIGHT):
        line = "  |"
        for mask, _ in col_data:
            line += "#" if mask & (1 << (DISPLAY_HEIGHT - 1 - row)) else " "
        line += "|"
        print(line)
    print()


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
    Builds a combined strip: [from_cols][gap][to_cols] and scrolls
    from the centre of from_cols to the centre of to_cols.
    """
    gap = [(0b0000000, COLOR_NONE)] * DISPLAY_WIDTH
    combined = from_cols + gap + to_cols

    start = centre_offset(from_cols)
    # to_cols starts after from_cols + gap in the combined strip
    to_start = len(from_cols) + len(gap)
    end = to_start + centre_offset(to_cols)

    for offset in range(start, end + 1):
        if hat:
            render_columns(hat, combined, offset)
        time.sleep(SCROLL_SPEED)


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

            # Cycle through labels: linger -> scroll -> linger -> scroll -> ...
            for i, (cols, color, m) in enumerate(labels):
                offset = centre_offset(cols)
                console_preview(cols, label=f"{m}m")

                # Hold this arrival on screen
                linger(hat, cols, offset, LINGER_SECS)

                # Re-fetch check before scrolling
                if time.time() - last_fetch >= REFRESH_SECS:
                    break

                # Scroll to the next label (wrap around to first after last)
                next_cols, _, _ = labels[(i + 1) % len(labels)]
                scroll_to_next(hat, cols, next_cols)

    except KeyboardInterrupt:
        print("\nExiting.")
        if hat:
            hat.clear()
            hat.show()


if __name__ == "__main__":
    main()
