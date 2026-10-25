"""
Expense store (SQLite) — expenses, budgets, settings, USD→INR rates.

Every amount is kept in the currency it was paid in plus its INR value (`inr`),
converted at that day's rate, so totals are always in rupees.
"""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
import threading
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")

CATEGORIES = {  # name → emoji; order is the order shown everywhere
    "Cloud & hosting": "☁️",
    "Software & AI": "🤖",
    "Domains & DNS": "🌐",
    "Hardware": "🔧",
    "Office": "🏢",
    "Travel": "🚗",
    "Food": "🍽️",
    "Marketing": "📣",
    "Freelance & salaries": "👥",
    "Fees & legal": "🧾",
    "Other": "📦",
}
CURRENCIES = ("INR", "USD")

# First guess of a category from the vendor name, used until the vendor has been seen before.
VENDOR_HINTS = [
    (r"hetzner|aws|amazon web|digitalocean|linode|vultr|azure|gcp|google cloud|vercel|netlify|render|railway", "Cloud & hosting"),
    (r"cloudflare|godaddy|namecheap|porkbun|domain|dns", "Domains & DNS"),
    (r"claude|anthropic|openai|chatgpt|github|copilot|notion|figma|slack|zoom|google workspace|gsuite|jetbrains|cursor|software|licen[cs]e", "Software & AI"),
    (r"raspberry|jetson|nvidia|robu|amazon|flipkart|camera|sd card|ssd|cable|sensor|arduino|hardware|laptop", "Hardware"),
    (r"uber|ola|rapido|flight|indigo|train|irctc|bus|petrol|fuel|taxi|hotel|toll|parking", "Travel"),
    (r"swiggy|zomato|tea|coffee|lunch|dinner|snack|restaurant|food|cafe", "Food"),
    (r"rent|electricity|internet|wifi|broadband|jio|airtel|stationery|print|office|courier", "Office"),
    (r"ads|linkedin|facebook|meta|instagram|marketing|banner|promo", "Marketing"),
    (r"freelanc|salary|contractor|intern|upwork|fiverr", "Freelance & salaries"),
    (r"gst|tax|ca |chartered|legal|bank|fee|stamp|roc|mca|trademark", "Fees & legal"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,                -- YYYY-MM-DD, the day it was paid
  amount REAL NOT NULL,              -- in `currency`
  currency TEXT NOT NULL DEFAULT 'INR',
  inr REAL NOT NULL,                 -- amount in rupees at that day's rate
  vendor TEXT NOT NULL,
  category TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  receipt TEXT,                      -- file name in data/receipts/
  source TEXT NOT NULL DEFAULT 'web',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS expenses_date ON expenses(date);
CREATE TABLE IF NOT EXISTS budgets (category TEXT PRIMARY KEY, monthly_inr REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fx (date TEXT PRIMARY KEY, usd_inr REAL NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- "already sent" markers
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def today() -> date:
    return datetime.now(TZ).date()


def month_of(d: str | date) -> str:
    return str(d)[:7]


def shift_month(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    m += n
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def week_start(d: date) -> date:
    """Monday of d's week."""
    return d - timedelta(days=d.weekday())


MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_date(text: str | None) -> date:
    """today / yesterday / 2026-10-20 / 20-10-2026 / 20/10 / 20 oct / oct 20 → date (default today)."""
    t = (text or "").strip().lower()
    now = today()
    if t in ("", "today", "now"):
        return now
    if t in ("yesterday", "yday"):
        return now - timedelta(days=1)
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", t):
        y = int(m[3]) if m[3] else now.year
        return date(y + 2000 if y < 100 else y, int(m[2]), int(m[1]))
    if m := re.fullmatch(r"(\d{1,2})\s*([a-z]{3})[a-z]*\.?\s*(\d{4})?", t) or \
            re.fullmatch(r"([a-z]{3})[a-z]*\.?\s*(\d{1,2})\s*,?\s*(\d{4})?", t):
        a, b, y = m[1], m[2], m[3]
        day, mon = (a, b) if a.isdigit() else (b, a)
        if mon in MONTHS:
            return date(int(y) if y else now.year, MONTHS[mon], int(day))
    raise ValueError(f"can't read the date {text!r}; use e.g. 2026-10-20, 20/10, 20 oct or yesterday")


def parse_month(text: str | None) -> str:
    """'' / this / last / 2026-10 / oct / october 2025 → YYYY-MM."""
    t = (text or "").strip().lower()
    cur = month_of(today())
    if t in ("", "this", "now", "current"):
        return cur
    if t in ("last", "previous", "prev"):
        return shift_month(cur, -1)
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", t):
        return f"{int(m[1]):04d}-{int(m[2]):02d}"
    if m := re.fullmatch(r"([a-z]{3})[a-z]*\s*(\d{4})?", t):
        if m[1] in MONTHS:
            mon = MONTHS[m[1]]
            y = int(m[2]) if m[2] else (today().year if f"{today().year}-{mon:02d}" <= cur else today().year - 1)
            return f"{y:04d}-{mon:02d}"
    raise ValueError(f"can't read the month {text!r}; use e.g. 2026-10, oct or last")


def inr(x: float) -> str:
    """₹ with Indian digit grouping: 1234567.5 → ₹12,34,567.50 (paise only when there are some)."""
    neg, x = x < 0, abs(round(x, 2))
    whole, frac = divmod(round(x * 100), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join(re.findall(r"\d{1,2}(?=(?:\d{2})*$)", head))
        s = f"{head},{tail}"
    return ("-" if neg else "") + "₹" + s + (f".{frac:02d}" if frac else "")


def money(amount: float, currency: str) -> str:
    return inr(amount) if currency == "INR" else f"${amount:,.2f}"


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        with self.lock, self.conn:
            return self.conn.execute(sql, args).lastrowid

    # ── state markers (so a reminder / alert is sent once) ──
    def once(self, key: str) -> bool:
        """True the first time a key is seen, False after."""
        with self.lock, self.conn:
            return self.conn.execute("INSERT OR IGNORE INTO state VALUES (?, ?)",
                                     (key, datetime.now(TZ).isoformat())).rowcount == 1

    # ── currency ──
    def usd_inr(self, d: date) -> float:
        """USD→INR on day d (ECB reference rate via frankfurter), cached; falls back to the latest known rate."""
        key = d.isoformat()
        if r := self.q("SELECT usd_inr FROM fx WHERE date = ?", (key,)):
            return r[0]["usd_inr"]
        day = "latest" if d >= today() else key
        for base in ("https://api.frankfurter.dev/v1", "https://api.frankfurter.app"):
            try:
                req = urllib.request.Request(f"{base}/{day}?base=USD&symbols=INR", headers={"User-Agent": "ddlc-expenses"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    rate = float(json.load(resp)["rates"]["INR"])
                self.x("INSERT OR REPLACE INTO fx VALUES (?, ?)", (key, rate))
                return rate
            except Exception:
                continue
        if r := self.q("SELECT usd_inr FROM fx ORDER BY ABS(julianday(date) - julianday(?)) LIMIT 1", (key,)):
            return r[0]["usd_inr"]
        raise ValueError("no USD→INR rate available right now; enter the amount in INR")

    def to_inr(self, amount: float, currency: str, d: date) -> float:
        return round(amount if currency == "INR" else amount * self.usd_inr(d), 2)

    # ── categories ──
    def guess_category(self, vendor: str) -> str:
        if r := self.q("SELECT category FROM expenses WHERE lower(vendor) = lower(?) ORDER BY id DESC LIMIT 1", (vendor,)):
            return r[0]["category"]
        v = vendor.lower()
        return next((cat for pat, cat in VENDOR_HINTS if re.search(pat, v)), "Other")

    def vendors(self, prefix: str = "", limit: int = 25) -> list[str]:
        return [r["vendor"] for r in self.q(
            "SELECT vendor, COUNT(*) n FROM expenses WHERE vendor LIKE ? GROUP BY lower(vendor) "
            "ORDER BY n DESC, MAX(id) DESC LIMIT ?", (f"%{prefix}%", limit))]

    # ── expenses ──
    def clean(self, e: dict) -> dict:
        """Validate + normalise an expense from the web or Discord."""
        vendor = str(e.get("vendor") or "").strip()
        if not vendor:
            raise ValueError("vendor is required")
        try:
            amount = round(float(str(e.get("amount")).replace(",", "").replace("₹", "").replace("$", "")), 2)
        except (TypeError, ValueError):
            raise ValueError("amount must be a number") from None
        if amount <= 0:
            raise ValueError("amount must be more than 0")
        currency = str(e.get("currency") or "INR").upper()
        if currency not in CURRENCIES:
            raise ValueError("currency must be INR or USD")
        d = e["date"] if isinstance(e.get("date"), date) else parse_date(e.get("date"))
        if d > today() + timedelta(days=1):
            raise ValueError("date is in the future")
        category = e.get("category") or self.guess_category(vendor)
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        return {"date": d.isoformat(), "amount": amount, "currency": currency, "inr": self.to_inr(amount, currency, d),
                "vendor": vendor[:80], "category": category, "note": str(e.get("note") or "").strip()[:300]}

    def add(self, e: dict, source: str = "web") -> dict:
        c = self.clean(e)
        eid = self.x("INSERT INTO expenses (date, amount, currency, inr, vendor, category, note, source, "
                     "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (c["date"], c["amount"], c["currency"], c["inr"], c["vendor"], c["category"], c["note"],
                      source, datetime.now(TZ).isoformat(timespec="seconds")))
        return self.get(eid)

    def update(self, eid: int, e: dict) -> dict:
        old = self.get(eid)
        if not old:
            raise KeyError(eid)
        c = self.clean({**old, **e})
        self.x("UPDATE expenses SET date=?, amount=?, currency=?, inr=?, vendor=?, category=?, note=? WHERE id=?",
               (c["date"], c["amount"], c["currency"], c["inr"], c["vendor"], c["category"], c["note"], eid))
        return self.get(eid)

    def delete(self, eid: int) -> dict | None:
        e = self.get(eid)
        if e:
            self.x("DELETE FROM expenses WHERE id = ?", (eid,))
        return e

    def get(self, eid: int) -> dict | None:
        r = self.q("SELECT * FROM expenses WHERE id = ?", (eid,))
        return r[0] if r else None

    def set_receipt(self, eid: int, name: str | None):
        self.x("UPDATE expenses SET receipt = ? WHERE id = ?", (name, eid))

    def list(self, month: str | None = None, q: str = "", category: str = "", start: str = "", end: str = "",
             limit: int = 5000) -> list[dict]:
        where, args = [], []
        if month:
            where.append("substr(date, 1, 7) = ?"); args.append(month)
        if start:
            where.append("date >= ?"); args.append(start)
        if end:
            where.append("date <= ?"); args.append(end)
        if category:
            where.append("category = ?"); args.append(category)
        if q:
            where.append("(vendor LIKE ? OR note LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
        sql = "SELECT * FROM expenses" + (" WHERE " + " AND ".join(where) if where else "")
        return self.q(sql + " ORDER BY date DESC, id DESC LIMIT ?", (*args, limit))

    def recent(self, n: int = 10) -> list[dict]:
        return self.q("SELECT * FROM expenses ORDER BY id DESC LIMIT ?", (n,))

    # ── budgets ──
    def budgets(self) -> dict[str, float]:
        return {r["category"]: r["monthly_inr"] for r in self.q("SELECT * FROM budgets")}

    def set_budget(self, category: str, amount: float):
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        if amount and amount > 0:
            self.x("INSERT OR REPLACE INTO budgets VALUES (?, ?)", (category, round(float(amount), 2)))
        else:
            self.x("DELETE FROM budgets WHERE category = ?", (category,))

    def month_total(self, month: str, category: str | None = None) -> float:
        sql, args = "SELECT COALESCE(SUM(inr), 0) t FROM expenses WHERE substr(date, 1, 7) = ?", [month]
        if category:
            sql += " AND category = ?"; args.append(category)
        return round(self.q(sql, args)[0]["t"], 2)

    # ── settings ──
    SETTINGS = {"weekly_report": "1", "monthly_report": "1"}  # defaults

    def settings(self) -> dict[str, str]:
        return {**self.SETTINGS, **{r["key"]: r["value"] for r in self.q("SELECT * FROM settings")}}

    def set_settings(self, values: dict):
        for k, v in values.items():
            if k not in self.SETTINGS:
                raise ValueError(f"unknown setting {k!r}")
            self.x("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, "1" if v in (True, 1, "1", "true") else "0"))

    # ── reports ──
    def summary(self, month: str) -> dict:
        prev = shift_month(month, -1)
        budgets = self.budgets()
        by_cat = {r["category"]: r for r in self.q(
            "SELECT category, ROUND(SUM(inr), 2) total, COUNT(*) count FROM expenses "
            "WHERE substr(date, 1, 7) = ? GROUP BY category", (month,))}
        prev_cat = {r["category"]: r["total"] for r in self.q(
            "SELECT category, ROUND(SUM(inr), 2) total FROM expenses WHERE substr(date, 1, 7) = ? GROUP BY category", (prev,))}
        cats = [{"category": c, "emoji": CATEGORIES[c], "total": by_cat.get(c, {}).get("total", 0),
                 "count": by_cat.get(c, {}).get("count", 0), "prev": prev_cat.get(c, 0), "budget": budgets.get(c)}
                for c in CATEGORIES if c in by_cat or c in budgets]
        cats.sort(key=lambda c: -c["total"])
        months = [shift_month(month, -i) for i in range(11, -1, -1)]
        totals = {r["m"]: r["t"] for r in self.q(
            "SELECT substr(date, 1, 7) m, ROUND(SUM(inr), 2) t FROM expenses WHERE substr(date, 1, 7) BETWEEN ? AND ? "
            "GROUP BY m", (months[0], months[-1]))}
        top = self.q("SELECT vendor, ROUND(SUM(inr), 2) total, COUNT(*) count FROM expenses "
                     "WHERE substr(date, 1, 7) = ? GROUP BY lower(vendor) ORDER BY total DESC LIMIT 5", (month,))
        return {
            "month": month, "today": today().isoformat(),
            "total": self.month_total(month), "prev_total": self.month_total(prev),
            "count": sum(c["count"] for c in cats),
            "categories": cats,
            "trend": [{"month": m, "total": totals.get(m, 0)} for m in months],
            "top_vendors": top,
            "budget_total": round(sum(budgets.values()), 2) if budgets else None,
            "budget_spent": round(sum(c["total"] for c in cats if c["budget"]), 2),  # only budgeted categories
        }

    def range_total(self, start: date, end: date) -> float:
        return round(self.q("SELECT COALESCE(SUM(inr), 0) t FROM expenses WHERE date BETWEEN ? AND ?",
                            (start.isoformat(), end.isoformat()))[0]["t"], 2)

    def week(self, start: date) -> dict:
        """Monday–Sunday week starting at `start`: total, vs the week before, categories, vendors, days."""
        end = start + timedelta(days=6)
        a, b = start.isoformat(), end.isoformat()
        cats = self.q("SELECT category, ROUND(SUM(inr), 2) total, COUNT(*) count FROM expenses "
                      "WHERE date BETWEEN ? AND ? GROUP BY category ORDER BY total DESC", (a, b))
        for c in cats:
            c["emoji"] = CATEGORIES.get(c["category"], "📦")
        per_day = {r["date"]: r["t"] for r in self.q(
            "SELECT date, ROUND(SUM(inr), 2) t FROM expenses WHERE date BETWEEN ? AND ? GROUP BY date", (a, b))}
        return {
            "start": a, "end": b,
            "total": self.range_total(start, end),
            "prev_total": self.range_total(start - timedelta(days=7), start - timedelta(days=1)),
            "count": sum(c["count"] for c in cats),
            "categories": cats,
            "top_vendors": self.q("SELECT vendor, ROUND(SUM(inr), 2) total, COUNT(*) count FROM expenses "
                                  "WHERE date BETWEEN ? AND ? GROUP BY lower(vendor) ORDER BY total DESC LIMIT 5", (a, b)),
            "biggest": self.q("SELECT * FROM expenses WHERE date BETWEEN ? AND ? ORDER BY inr DESC LIMIT 3", (a, b)),
            "days": [{"date": (start + timedelta(days=i)).isoformat(),
                      "total": per_day.get((start + timedelta(days=i)).isoformat(), 0)} for i in range(7)],
            "month_total": self.month_total(month_of(end)),
        }

    def backup(self, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            out = sqlite3.connect(dest)
            self.conn.backup(out)
            out.close()
