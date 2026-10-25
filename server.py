#!/usr/bin/env python3
"""
Run the expense tracker on this machine: the web page (web/) and the API (backend/) on one port.

  set -a; . ./.env; set +a
  python3 server.py                      # http://localhost:9200/
  python3 server.py --port 9200 --data data

Data is kept in data/store.json and data/receipts/. On AWS the same API runs in Lambda
(backend/lambda_function.py) with DynamoDB and S3; see deploy/.
"""

import argparse
import mimetypes
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
WEB = (HERE / "web").resolve()
sys.path.insert(0, str(HERE / "backend"))

from api import App  # noqa: E402
from storage import FileBackend, LocalReceipts  # noqa: E402

SCHEDULE_S = 300


def main():
    parser = argparse.ArgumentParser(description="DDLC expense tracker (local)")
    parser.add_argument("--port", type=int, default=9200)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--data", type=Path, default=HERE / "data")
    args = parser.parse_args()

    app = App(FileBackend(args.data / "store.json"), LocalReceipts(args.data / "receipts"),
              os.environ.get("DASHBOARD_PASSWORD"), os.environ.get("TOKEN_SECRET") or secrets.token_hex(32))

    def timer():
        while True:
            try:
                app.scheduled()
            except Exception as e:
                print("scheduled:", e)
            time.sleep(SCHEDULE_S)
    threading.Thread(target=timer, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _any(self):
            u = urlparse(self.path)
            if self.command == "GET" and u.path == "/config.js":  # same origin: the API is on this port
                return self._send(200, "text/javascript", b'window.EXPENSES_API = "";')
            if self.command == "GET" and not u.path.startswith("/api/"):  # the page, manifest, icons, sw.js
                f = (WEB / (u.path.lstrip("/") or "index.html")).resolve()
                if f.is_file() and WEB in f.parents:
                    return self._send(200, mimetypes.guess_type(f.name)[0] or "application/octet-stream", f.read_bytes())
                return self._send(404, "text/plain", b"not found")
            n = int(self.headers.get("Content-Length") or 0)
            r = app.handle(self.command, u.path, {k: v[0] for k, v in parse_qs(u.query).items()},
                           {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(n) if n else b"")
            self._send(r.status, r.ctype, r.body, r.headers)

        do_GET = do_POST = do_PUT = do_DELETE = _any

        def _send(self, code, ctype, body, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    print(f"Expense tracker on http://{args.bind}:{args.port}/ — login {'on' if app.password else 'off'}")
    server.serve_forever()


if __name__ == "__main__":
    main()
