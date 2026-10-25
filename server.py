#!/usr/bin/env python3
"""
DDLC expense tracker — web dashboard + Discord webhook notifications.

  python3 server.py                      # http://0.0.0.0:9200/
  python3 server.py --port 9200 --data data

  GET  /                              UI (index.html)
  POST /api/login                     {"password": …} → {"token": …}
  GET  /api/meta                      categories, currencies, known vendors, Discord on/off
  GET  /api/summary?month=YYYY-MM     month total, categories + budgets, 12-month trend, upcoming renewals
  GET  /api/expenses?month=&q=&category=
  POST /api/expenses                  {date, amount, currency, vendor, category, note}
  PUT / DELETE /api/expenses/<id>
  PUT / DELETE /api/expenses/<id>/receipt     body = the image / PDF, X-Filename header
  GET  /api/receipts/<id>?token=…
  GET  /api/week?week=this|last|YYYY-MM-DD   Monday–Sunday totals
  GET / PUT  /api/settings            {"weekly_report": "1", "monthly_report": "1"}
  GET / PUT  /api/budgets             {"Cloud & hosting": 10000, …} (0 removes)
  GET  /api/export.csv?from=&to=&token=…
  POST /api/discord/test, /api/discord/report {"month": …}, /api/discord/weekly {"week": "this"|"last"}

Settings come from .env (systemd EnvironmentFile): DASHBOARD_PASSWORD, TOKEN_SECRET,
DISCORD_WEBHOOK_URL, PUBLIC_URL. Data (SQLite, receipts, daily backups) lives in data/.
Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from db import CATEGORIES, CURRENCIES, TZ, Store, parse_date, parse_month, today, week_start
from notify import Notifier

HERE = Path(__file__).parent
TOKEN_DAYS = 30
SCHEDULE_S = 300
MAX_RECEIPT = 10 * 1024 * 1024
RECEIPT_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
                 "application/pdf": ".pdf"}
KEEP_BACKUPS = 30

PASSWORD = os.environ.get("DASHBOARD_PASSWORD") or None
TOKEN_SECRET = os.environ.get("TOKEN_SECRET") or secrets.token_hex(32)


def _sign(msg: str) -> str:
    return hmac.new(TOKEN_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    exp = str(int(time.time()) + TOKEN_DAYS * 86400)
    return f"{exp}.{_sign(exp)}"


def token_ok(token: str | None) -> bool:
    if PASSWORD is None:
        return True
    exp, _, sig = (token or "").partition(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp))


def scheduler(store: Store, notifier: Notifier, data: Path):
    """Every few minutes: Monday weekly report, 1st-of-month report; once a day: backup."""
    while True:
        try:
            if notifier.enabled:
                notifier.daily()
            store.usd_inr(today())  # warm the day's rate so adding a USD expense is instant
        except Exception as e:
            print("scheduler:", e)
        backups = data / "backups"
        dest = backups / f"expenses-{today().isoformat()}.db"
        if not dest.exists():
            try:
                store.backup(dest)
                for old in sorted(backups.glob("expenses-*.db"))[:-KEEP_BACKUPS]:
                    old.unlink()
            except Exception as e:
                print("backup:", e)
        time.sleep(SCHEDULE_S)


def main():
    parser = argparse.ArgumentParser(description="DDLC expense tracker")
    parser.add_argument("--port", type=int, default=9200)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--data", type=Path, default=HERE / "data")
    args = parser.parse_args()

    store = Store(args.data / "expenses.db")
    receipts = args.data / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    notifier = Notifier(store)
    threading.Thread(target=scheduler, args=(store, notifier, args.data), daemon=True).start()

    def drop_receipt(e: dict | None):
        if e and e.get("receipt"):
            (receipts / e["receipt"]).unlink(missing_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # ── plumbing ──
        def _route(self, method: str):
            u = urlparse(self.path)
            self.qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path.rstrip("/") or "/"
            if method == "GET" and path == "/":
                return self._send(200, "text/html; charset=utf-8", (HERE / "index.html").read_bytes())
            if method == "POST" and path == "/api/login":
                return self.login()
            if not path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            auth = self.headers.get("Authorization", "")
            if not token_ok(auth[7:] if auth.startswith("Bearer ") else self.qs.get("token")):
                return self._json({"error": "login required", "login": True}, 401)
            try:
                self.api(method, path[5:])
            except KeyError as e:
                self._json({"error": f"not found: {e}"}, 404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

        def body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_RECEIPT:
                raise ValueError("file is over 10 MB")
            return self.rfile.read(n) if n else b""

        def jbody(self) -> dict:
            try:
                return json.loads(self.body() or b"{}")
            except json.JSONDecodeError:
                raise ValueError("bad JSON") from None

        def login(self):
            try:
                body = self.jbody()
            except ValueError:
                body = {}
            if PASSWORD is None:
                self._json({"token": "", "days": 0})
            elif hmac.compare_digest(str(body.get("password", "")), PASSWORD):
                self._json({"token": make_token(), "days": TOKEN_DAYS})
            else:
                time.sleep(1)  # slow down guessing
                self._json({"error": "wrong password", "login": True}, 401)

        # ── API ──
        def api(self, method: str, path: str):
            m = re.fullmatch(r"(expenses|receipts)/(\d+)(/receipt)?", path)
            kind, rid, sub = (m[1], int(m[2]), m[3]) if m else (path, None, None)

            if (method, path) == ("GET", "meta"):
                return self._json({"categories": [{"name": c, "emoji": e} for c, e in CATEGORIES.items()],
                                   "currencies": CURRENCIES, "vendors": store.vendors(limit=200),
                                   "today": today().isoformat(), "discord": notifier.enabled,
                                   "settings": store.settings(),
                                   "login": PASSWORD is not None})
            if (method, path) == ("GET", "summary"):
                return self._json(store.summary(parse_month(self.qs.get("month"))))
            if (method, path) == ("GET", "week"):
                return self._json(store.week(self.week_of(self.qs.get("week"))))

            if kind == "expenses" and rid is None:
                if method == "GET":
                    month = parse_month(self.qs["month"]) if self.qs.get("month") else None
                    return self._json(store.list(month, self.qs.get("q", ""), self.qs.get("category", "")))
                if method == "POST":
                    e = store.add(self.jbody(), source="web")
                    notifier.expense_added(e)
                    return self._json(e, 201)
            if kind == "expenses" and sub:
                e = store.get(rid) or {}
                if not e:
                    raise KeyError(rid)
                if method == "PUT":
                    ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    ext = RECEIPT_TYPES.get(ctype) or Path(self.headers.get("X-Filename", "")).suffix.lower()
                    if ext not in RECEIPT_TYPES.values():
                        raise ValueError("receipt must be a photo (JPG, PNG, WebP, HEIC) or a PDF")
                    data = self.body()
                    if not data:
                        raise ValueError("empty file")
                    drop_receipt(e)
                    name = f"{rid}-{secrets.token_hex(4)}{ext}"
                    (receipts / name).write_bytes(data)
                    store.set_receipt(rid, name)
                    return self._json(store.get(rid))
                if method == "DELETE":
                    drop_receipt(e)
                    store.set_receipt(rid, None)
                    return self._json(store.get(rid))
            if kind == "expenses" and rid is not None:
                if method == "PUT":
                    return self._json(store.update(rid, self.jbody()))
                if method == "DELETE":
                    e = store.delete(rid)
                    if not e:
                        raise KeyError(rid)
                    drop_receipt(e)
                    return self._json({"deleted": rid})
            if (method, kind) == ("GET", "receipts") and rid is not None:
                e = store.get(rid)
                if not e or not e.get("receipt") or not (receipts / e["receipt"]).exists():
                    raise KeyError(rid)
                f = receipts / e["receipt"]
                return self._send(200, mimetypes.guess_type(f.name)[0] or "application/octet-stream", f.read_bytes(),
                                  {"Content-Disposition": f'inline; filename="receipt-{rid}{f.suffix}"'})

            if path == "settings":
                if method == "PUT":
                    store.set_settings(self.jbody())
                if method in ("GET", "PUT"):
                    return self._json(store.settings())

            if path == "budgets":
                if method == "PUT":
                    for cat, amount in self.jbody().items():
                        store.set_budget(cat, float(amount or 0))
                if method in ("GET", "PUT"):
                    return self._json(store.budgets())

            if (method, path) == ("GET", "export.csv"):
                return self.export()
            if (method, path) == ("POST", "discord/test"):
                err = notifier.post([{"title": "✅ DDLC Expenses is connected", "color": 0x0CA30C,
                                      "description": "New expenses, budget alerts and the weekly and monthly "
                                                     "reports will be posted here."}], wait=True)
                return self._json({"error": err}, 502) if err else self._json({"sent": True})
            if (method, path) == ("POST", "discord/report"):
                month = parse_month(self.jbody().get("month"))
                err = notifier.post([notifier.report_embed(month)], wait=True)
                return self._json({"error": err}, 502) if err else self._json({"sent": True})
            if (method, path) == ("POST", "discord/weekly"):
                err = notifier.post([notifier.week_embed(self.week_of(self.jbody().get("week")))], wait=True)
                return self._json({"error": err}, 502) if err else self._json({"sent": True})
            raise KeyError(path)

        @staticmethod
        def week_of(text: str | None) -> date:
            """this (default) / last / any YYYY-MM-DD → Monday of that week."""
            t = (text or "this").strip().lower()
            d = today() - timedelta(days=7) if t == "last" else today() if t == "this" else parse_date(t)
            return week_start(d)

        def export(self):
            start, end = self.qs.get("from", ""), self.qs.get("to", "")
            rows = store.list(start=start, end=end, category=self.qs.get("category", ""))
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["id", "date", "vendor", "category", "amount", "currency", "amount_inr", "note", "receipt", "source"])
            for e in reversed(rows):
                w.writerow([e["id"], e["date"], e["vendor"], e["category"], e["amount"], e["currency"], e["inr"],
                            e["note"], "yes" if e["receipt"] else "", e["source"]])
            label = f"{start or 'start'}_to_{end or today().isoformat()}"
            self._send(200, "text/csv; charset=utf-8", ("﻿" + out.getvalue()).encode(),  # BOM: Excel opens ₹ right
                       {"Content-Disposition": f'attachment; filename="ddlc-expenses-{label}.csv"'})

        def _json(self, obj, code=200):
            self._send(code, "application/json", json.dumps(obj).encode())

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
    print(f"Expense tracker on http://{args.bind}:{args.port}/ — login {'on' if PASSWORD else 'off'}, "
          f"Discord {'on' if notifier.enabled else 'off'}, {datetime.now(TZ):%Y-%m-%d %H:%M %Z}")
    server.serve_forever()


if __name__ == "__main__":
    main()
