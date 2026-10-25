"""
The expense tracker's HTTP API, independent of where it runs (Lambda or server.py).

  POST /api/login                     {"password": …} → {"token": …}
  GET  /api/meta                      categories, currencies, known vendors, Discord on/off, settings
  GET  /api/summary?month=YYYY-MM     month total, categories + budgets, 12-month trend, top vendors
  GET  /api/week?week=this|last|YYYY-MM-DD   Monday–Sunday totals
  GET  /api/expenses?month=&q=&category=
  POST /api/expenses                  {date, amount, currency, vendor, category, note}
  PUT / DELETE /api/expenses/<id>
  POST /api/expenses/<id>/receipt-upload  {type, name, size} → {url, headers, key}; the browser PUTs the file to url
  PUT  /api/expenses/<id>/receipt     {key} attaches the uploaded file; DELETE removes it
  GET  /api/receipts/<id>?token=…     the file (local) or a redirect to it (S3)
  GET / PUT  /api/budgets             {"Food": 2000, …} (0 removes)
  GET / PUT  /api/settings            {"weekly_report": "1", "monthly_report": "1"}
  GET  /api/export.csv?from=&to=&token=…
  POST /api/discord/test, /api/discord/report {"month"}, /api/discord/weekly {"week": "this"|"last"}

Every route but login needs "Authorization: Bearer <token>" (or ?token= for links).
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import time
from datetime import date, timedelta
from pathlib import Path

from core import CATEGORIES, CURRENCIES, Store, parse_date, parse_month, today, week_start
from notify import Notifier
from storage import CONTENT_TYPES, MAX_RECEIPT, RECEIPT_TYPES

TOKEN_DAYS = 30


class Response:
    def __init__(self, status: int, body: bytes | str | dict | list = b"", ctype: str = "application/json",
                 headers: dict | None = None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        self.status, self.ctype, self.headers = status, ctype, headers or {}
        self.body = body.encode() if isinstance(body, str) else body


class App:
    def __init__(self, backend, receipts, password: str | None, token_secret: str, background_posts: bool = True):
        self.backend, self.receipts = backend, receipts
        self.password = password or None
        self.secret = token_secret
        self.background_posts = background_posts

    # ── login tokens ──
    def _sign(self, msg: str) -> str:
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def make_token(self) -> str:
        exp = str(int(time.time()) + TOKEN_DAYS * 86400)
        return f"{exp}.{self._sign(exp)}"

    def token_ok(self, token: str | None) -> bool:
        if self.password is None:
            return True
        exp, _, sig = (token or "").partition(".")
        return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, self._sign(exp))

    # ── entry points ──
    def scheduled(self):
        """Daily timer: Monday weekly report, 1st-of-month report; keeps today's USD rate warm."""
        store = Store(self.backend)
        notifier = Notifier(store, background=self.background_posts)
        if notifier.enabled:
            notifier.daily()
        try:
            store.usd_inr(today())
        except ValueError:
            pass

    def handle(self, method: str, path: str, qs: dict, headers: dict, body: bytes) -> Response:
        """headers: lower-case names. qs: single values."""
        path = path.rstrip("/") or "/"
        if method == "POST" and path == "/api/login":
            return self.login(body)
        if not path.startswith("/api/"):
            return Response(404, {"error": "not found"})
        auth = headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else qs.get("token")
        if not self.token_ok(token):
            return Response(401, {"error": "login required", "login": True})
        req = Request(self, method, path[5:], qs, body, token or "")
        try:
            return req.route()
        except KeyError as e:
            return Response(404, {"error": f"not found: {e}"})
        except ValueError as e:
            return Response(400, {"error": str(e)})

    def login(self, body: bytes) -> Response:
        try:
            pw = str(json.loads(body or b"{}").get("password", ""))
        except (ValueError, AttributeError):
            pw = ""
        if self.password is None:
            return Response(200, {"token": "", "days": 0})
        if hmac.compare_digest(pw.encode(), self.password.encode()):
            return Response(200, {"token": self.make_token(), "days": TOKEN_DAYS})
        time.sleep(1)  # slow down guessing
        return Response(401, {"error": "wrong password", "login": True})


class Request:
    """One API call: a fresh Store (expenses loaded once) and Notifier."""

    def __init__(self, app: App, method: str, path: str, qs: dict, body: bytes, token: str):
        self.app, self.method, self.path, self.qs, self.raw, self.token = app, method, path, qs, body, token
        self.store = Store(app.backend)
        self.notifier = Notifier(self.store, background=app.background_posts)
        self.receipts = app.receipts

    def json(self) -> dict:
        try:
            out = json.loads(self.raw or b"{}")
        except ValueError:
            raise ValueError("bad JSON") from None
        if not isinstance(out, dict):
            raise ValueError("expected a JSON object")
        return out

    def route(self) -> Response:
        method, path, store = self.method, self.path, self.store
        m = re.fullmatch(r"(expenses|receipts)/(\d+)(/receipt|/receipt-upload)?", path)
        kind, rid, sub = (m[1], int(m[2]), m[3]) if m else (path, None, None)

        if (method, path) == ("GET", "meta"):
            return Response(200, {"categories": [{"name": c, "emoji": e} for c, e in CATEGORIES.items()],
                                  "currencies": CURRENCIES, "vendors": store.vendors(),
                                  "today": today().isoformat(), "discord": self.notifier.enabled,
                                  "settings": store.settings(), "login": self.app.password is not None})
        if (method, path) == ("GET", "summary"):
            return Response(200, store.summary(parse_month(self.qs.get("month"))))
        if (method, path) == ("GET", "week"):
            return Response(200, store.week(self.week_of(self.qs.get("week"))))

        if kind == "expenses" and rid is None:
            if method == "GET":
                month = parse_month(self.qs["month"]) if self.qs.get("month") else None
                return Response(200, store.list(month, self.qs.get("q", ""), self.qs.get("category", "")))
            if method == "POST":
                e = store.add(self.json(), source="web")
                self.notifier.expense_added(e)
                return Response(201, e)
        if kind == "expenses" and sub == "/receipt-upload" and method == "POST":
            if not store.get(rid):
                raise KeyError(rid)
            info = self.json()
            ctype = str(info.get("type") or "").lower()
            ext = RECEIPT_TYPES.get(ctype) or Path(str(info.get("name") or "")).suffix.lower()
            if ext not in CONTENT_TYPES:
                raise ValueError("receipt must be a photo (JPG, PNG, WebP, HEIC) or a PDF")
            if int(info.get("size") or 0) > MAX_RECEIPT:
                raise ValueError("receipt is over 10 MB")
            key = f"receipts/{rid}-{secrets.token_hex(6)}{'.jpg' if ext == '.jpeg' else ext}"
            return Response(200, {"key": key, **self.receipts.upload_target(key, CONTENT_TYPES[ext], self.token)})
        if kind == "expenses" and sub == "/receipt":
            e = store.get(rid)
            if not e:
                raise KeyError(rid)
            if method == "PUT":
                key = str(self.json().get("key") or "")
                if not key.startswith(f"receipts/{rid}-") or not self.receipts.exists(key):
                    raise ValueError("receipt upload not found; try again")
                if e.get("receipt") and e["receipt"] != key:
                    self.receipts.delete(e["receipt"])
                return Response(200, store.set_receipt(rid, key))
            if method == "DELETE":
                if e.get("receipt"):
                    self.receipts.delete(e["receipt"])
                return Response(200, store.set_receipt(rid, None))
        if kind == "expenses" and rid is not None and not sub:
            if method == "PUT":
                return Response(200, store.update(rid, self.json()))
            if method == "DELETE":
                e = store.delete(rid)
                if not e:
                    raise KeyError(rid)
                if e.get("receipt"):
                    self.receipts.delete(e["receipt"])
                return Response(200, {"deleted": rid})
        if (method, kind) == ("GET", "receipts") and rid is not None:
            e = store.get(rid)
            if not e or not e.get("receipt") or not self.receipts.exists(e["receipt"]):
                raise KeyError(rid)
            key = e["receipt"]
            name = f"receipt-{rid}{Path(key).suffix}"
            if hasattr(self.receipts, "view_url"):
                return Response(302, b"", "text/plain", {"Location": self.receipts.view_url(key, name)})
            return Response(200, self.receipts.read(key), CONTENT_TYPES.get(Path(key).suffix, "application/octet-stream"),
                            {"Content-Disposition": f'inline; filename="{name}"'})
        if (method, path) == ("PUT", "upload") and hasattr(self.receipts, "save"):  # local only: stands in for S3
            key = self.qs.get("key", "")
            if not re.fullmatch(r"receipts/\d+-[0-9a-f]+\.\w+", key):
                raise ValueError("bad key")
            if len(self.raw) > MAX_RECEIPT:
                raise ValueError("receipt is over 10 MB")
            self.receipts.save(key, self.raw)
            return Response(200, {"saved": key})

        if path == "budgets" and method in ("GET", "PUT"):
            if method == "PUT":
                store.set_budgets(self.json())
            return Response(200, store.budgets())
        if path == "settings" and method in ("GET", "PUT"):
            if method == "PUT":
                store.set_settings(self.json())
            return Response(200, store.settings())

        if (method, path) == ("GET", "export.csv"):
            return self.export()
        if method == "POST" and path.startswith("discord/"):
            n = self.notifier
            if path == "discord/test":
                embeds = [{"title": "✅ DDLC Expenses is connected", "color": 0x0CA30C,
                           "description": "New expenses, budget alerts and the weekly and monthly reports "
                                          "will be posted here."}]
            elif path == "discord/report":
                embeds = [n.report_embed(parse_month(self.json().get("month")))]
            elif path == "discord/weekly":
                embeds = [n.week_embed(self.week_of(self.json().get("week")))]
            else:
                raise KeyError(path)
            err = n.post(embeds, wait=True)
            return Response(502, {"error": err}) if err else Response(200, {"sent": True})
        raise KeyError(path)

    @staticmethod
    def week_of(text: str | None) -> date:
        """this (default) / last / any YYYY-MM-DD → Monday of that week."""
        t = (text or "this").strip().lower()
        d = today() - timedelta(days=7) if t == "last" else today() if t == "this" else parse_date(t)
        return week_start(d)

    def export(self) -> Response:
        start, end = self.qs.get("from", ""), self.qs.get("to", "")
        rows = self.store.list(start=start, end=end, category=self.qs.get("category", ""))
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["id", "date", "vendor", "category", "amount", "currency", "amount_inr", "note", "receipt", "source"])
        for e in reversed(rows):
            w.writerow([e["id"], e["date"], e["vendor"], e["category"], e["amount"], e["currency"], e["inr"],
                        e["note"], "yes" if e.get("receipt") else "", e["source"]])
        label = f"{start or 'start'}_to_{end or today().isoformat()}"
        return Response(200, "﻿" + out.getvalue(), "text/csv; charset=utf-8",  # BOM: Excel opens ₹ right
                        {"Content-Disposition": f'attachment; filename="ddlc-expenses-{label}.csv"'})
