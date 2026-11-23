#!/usr/bin/env python3
"""Receive temperature readings over HTTP and store them in SQLite.

Endpoints:
  GET  /                  dashboard page (dashboard.html)
  POST /readings          JSON body {"device", "temperature_c", "timestamp"}, needs X-API-Key
  GET  /readings?limit=N  latest readings as JSON (default 100, max 1000)
  GET  /health            liveness check

Standard library only. Run behind HTTPS (for example a reverse proxy such as Caddy or nginx)
before exposing it to the internet.
"""
import argparse
import hmac
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

MAX_BODY = 4096
MIN_TEMP_C, MAX_TEMP_C = -55.0, 125.0  # DS18B20 operating range

_db_lock = threading.Lock()
DB_PATH = "readings.db"
API_KEY = ""
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), "rb") as _f:
    DASHBOARD_HTML = _f.read()


def init_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS readings (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   device TEXT NOT NULL,
                   temperature_c REAL NOT NULL,
                   measured_at TEXT NOT NULL,
                   received_at TEXT NOT NULL
               )"""
        )


def save_reading(device, temp_c, measured_at):
    received = datetime.now(timezone.utc).isoformat()
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO readings (device, temperature_c, measured_at, received_at) VALUES (?, ?, ?, ?)",
            (device, temp_c, measured_at, received),
        )


def latest_readings(limit):
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT device, temperature_c, measured_at, received_at FROM readings ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"device": d, "temperature_c": t, "measured_at": m, "received_at": r}
        for d, t, m, r in rows
    ]


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            data = DASHBOARD_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)
        elif url.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif url.path == "/readings":
            try:
                limit = int(parse_qs(url.query).get("limit", ["100"])[0])
            except ValueError:
                return self._send_json(400, {"error": "limit must be an integer"})
            limit = max(1, min(limit, 1000))
            self._send_json(200, latest_readings(limit))
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/readings":
            return self._send_json(404, {"error": "not found"})

        if not hmac.compare_digest(self.headers.get("X-API-Key", ""), API_KEY):
            return self._send_json(401, {"error": "invalid API key"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send_json(400, {"error": "bad Content-Length"})
        if length <= 0 or length > MAX_BODY:
            return self._send_json(413, {"error": "body missing or too large"})

        try:
            payload = json.loads(self.rfile.read(length))
            device = str(payload["device"])[:64]
            temp_c = float(payload["temperature_c"])
            measured_at = str(payload["timestamp"])[:64]
        except (ValueError, KeyError, TypeError):
            return self._send_json(400, {"error": "expected device, temperature_c and timestamp"})

        if not (MIN_TEMP_C <= temp_c <= MAX_TEMP_C):
            return self._send_json(422, {"error": "temperature outside sensor range"})

        save_reading(device, temp_c, measured_at)
        self._send_json(201, {"status": "stored"})

    def log_message(self, fmt, *args):
        # Log to stderr without the API key or payload contents.
        print(f"{self.address_string()} {fmt % args}")


def main():
    global DB_PATH, API_KEY
    parser = argparse.ArgumentParser(description="Receive and store temperature readings.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="readings.db", help="SQLite database file")
    parser.add_argument("--api-key", default=os.environ.get("RECEIVER_API_KEY"),
                        help="Shared key (or set RECEIVER_API_KEY)")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("an API key is required (--api-key or RECEIVER_API_KEY)")

    DB_PATH = args.db
    API_KEY = args.api_key
    init_db(DB_PATH)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listening on {args.host}:{args.port}, storing in {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
