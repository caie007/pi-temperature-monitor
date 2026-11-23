#!/usr/bin/env python3
"""Read a DS18B20 temperature sensor on a Raspberry Pi and POST readings to a receiver.

Uses only the Python standard library. Readings that fail to send are kept in
memory and retried on the next cycle, so short internet outages do not lose data.
"""
import argparse
import glob
import json
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone

W1_GLOB = "/sys/bus/w1/devices/28-*/w1_slave"
MAX_BUFFER = 1000


def read_ds18b20(path=None):
    """Return the temperature in degrees Celsius from a DS18B20 sensor."""
    if path is None:
        matches = glob.glob(W1_GLOB)
        if not matches:
            raise RuntimeError("No DS18B20 found. Is 1-Wire enabled and the wiring correct?")
        path = matches[0]
    with open(path) as f:
        lines = f.read().splitlines()
    if len(lines) < 2 or not lines[0].endswith("YES"):
        raise RuntimeError("CRC check failed while reading the sensor")
    pos = lines[1].find("t=")
    if pos == -1:
        raise RuntimeError("No temperature value in sensor output")
    return int(lines[1][pos + 2:]) / 1000.0


def simulated_reading():
    """Stand-in for the sensor, for testing on a computer without hardware."""
    return 20.0 + random.uniform(-1.5, 1.5)


def build_payload(temp_c, device_id):
    return {
        "device": device_id,
        "temperature_c": round(temp_c, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def post_reading(url, api_key, payload, timeout=10):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def flush(buffer, url, api_key):
    """Send buffered readings oldest first. Stop at the first failure."""
    sent = 0
    while buffer:
        try:
            post_reading(url, api_key, buffer[0])
        except (urllib.error.URLError, OSError) as exc:
            print(f"send failed, will retry ({len(buffer)} queued): {exc}", file=sys.stderr)
            break
        buffer.popleft()
        sent += 1
    return sent


def main():
    parser = argparse.ArgumentParser(description="Send temperature readings to a receiver.")
    parser.add_argument("--url", required=True, help="Receiver endpoint, e.g. http://example.com:8000/readings")
    parser.add_argument("--api-key", default=os.environ.get("TEMP_API_KEY"),
                        help="Shared key (or set TEMP_API_KEY)")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between readings (default 60)")
    parser.add_argument("--device", default=socket.gethostname(), help="Device name sent with each reading")
    parser.add_argument("--sensor", default=None, help="Path to a w1_slave file (default: auto-detect)")
    parser.add_argument("--simulate", action="store_true", help="Use fake readings (no sensor needed)")
    parser.add_argument("--once", action="store_true", help="Send one reading and exit")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("an API key is required (--api-key or TEMP_API_KEY)")

    buffer = deque(maxlen=MAX_BUFFER)
    while True:
        try:
            temp = simulated_reading() if args.simulate else read_ds18b20(args.sensor)
            buffer.append(build_payload(temp, args.device))
            print(f"{args.device}: {temp:.2f} C")
        except RuntimeError as exc:
            print(f"sensor error: {exc}", file=sys.stderr)

        flush(buffer, args.url, args.api_key)

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
