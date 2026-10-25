"""
Expense logic — validation, totals, reports, budgets, settings, USD→INR rates.

Storage is a backend from storage.py: DynamoDB on AWS Lambda, a JSON file locally.
Every amount is kept in the currency it was paid in plus its INR value (`inr`),
converted at that day's rate, so totals are always in rupees.
"""

from __future__ import annotations

import calendar
import json
import re
import urllib.request
from datetime import date, datetime, timedelta
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


def num(x) -> float:
    """Amount from user input: 1,200 / ₹450 / $12.99 → float."""
    try:
        return round(float(str(x).replace(",", "").replace("₹", "").replace("$", "").strip()), 2)
    except (TypeError, ValueError):
        raise ValueError("amount must be a number") from None


class Store:
    """Expenses and their reports over a storage backend (see storage.py). One per request."""

    SETTINGS = {"weekly_report": "1", "monthly_report": "1"}  # defaults

    def __init__(self, backend):
        self.b = backend
        self._all: list[dict] | None = None  # every expense, loaded once per request

    def once(self, key: str) -> bool:
        """True the first time a key is seen, False after (a report or alert is sent once)."""
        return self.b.once(key)

    # ── currency ──
    def usd_inr(self, d: date) -> float:
        """USD→INR on day d (ECB reference rate via frankfurter), cached; falls back to the latest known rate."""
        key = d.isoformat()
        cached = self.b.kv_get("fx") or {}
        if key in cached:
            return cached[key]
        day = "latest" if d >= today() else key
        for base in ("https://api.frankfurter.dev/v1", "https://api.frankfurter.app"):
            try:
                req = urllib.request.Request(f"{base}/{day}?base=USD&symbols=INR", headers={"User-Agent": "ddlc-expenses"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    rate = float(json.load(resp)["rates"]["INR"])
                cached[key] = rate
                self.b.kv_put("fx", dict(sorted(cached.items())[-400:]))
                return rate
            except Exception:
                continue
        if cached:
            nearest = min(cached, key=lambda k: abs((date.fromisoformat(k) - d).days))
            return cached[nearest]
        raise ValueError("no USD→INR rate available right now; enter the amount in INR")

    def to_inr(self, amount: float, currency: str, d: date) -> float:
        return round(amount if currency == "INR" else amount * self.usd_inr(d), 2)

    # ── expenses ──
    def all(self) -> list[dict]:
        if self._all is None:
            self._all = sorted(self.b.expenses(), key=lambda e: (e["date"], e["id"]), reverse=True)
        return self._all

    def get(self, eid: int) -> dict | None:
        return self.b.get_expense(eid)

    def guess_category(self, vendor: str) -> str:
        v = vendor.lower()
        seen = next((e["category"] for e in sorted(self.all(), key=lambda e: -e["id"]) if e["vendor"].lower() == v), None)
        return seen or next((cat for pat, cat in VENDOR_HINTS if re.search(pat, v)), "Other")

    def vendors(self, limit: int = 200) -> list[str]:
        counts: dict[str, list] = {}
        for e in self.all():
            c = counts.setdefault(e["vendor"].lower(), [0, 0, e["vendor"]])
            c[0] += 1
            c[1] = max(c[1], e["id"])
        return [c[2] for c in sorted(counts.values(), key=lambda c: (-c[0], -c[1]))][:limit]

    def clean(self, e: dict) -> dict:
        """Validate + normalise an expense from the web."""
        vendor = str(e.get("vendor") or "").strip()
        if not vendor:
            raise ValueError("vendor is required")
        amount = num(e.get("amount"))
        if amount <= 0:
            raise ValueError("amount must be more than 0")
        currency = str(e.get("currency") or "INR").upper()
        if currency not in CURRENCIES:
            raise ValueError("currency must be INR or USD")
        d = parse_date(e.get("date"))
        if d > today() + timedelta(days=1):
            raise ValueError("date is in the future")
        category = e.get("category") or self.guess_category(vendor)
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        return {"date": d.isoformat(), "amount": amount, "currency": currency, "inr": self.to_inr(amount, currency, d),
                "vendor": vendor[:80], "category": category, "note": str(e.get("note") or "").strip()[:300]}

    def add(self, e: dict, source: str = "web") -> dict:
        doc = {"id": self.b.next_id(), **self.clean(e), "receipt": None, "source": source,
               "created_at": datetime.now(TZ).isoformat(timespec="seconds")}
        self.b.put_expense(doc)
        self._all = None
        return doc

    def update(self, eid: int, e: dict) -> dict:
        old = self.get(eid)
        if not old:
            raise KeyError(eid)
        doc = {**old, **self.clean({**old, **e})}
        self.b.put_expense(doc)
        self._all = None
        return doc

    def delete(self, eid: int) -> dict | None:
        e = self.get(eid)
        if e:
            self.b.delete_expense(eid)
            self._all = None
        return e

    def set_receipt(self, eid: int, key: str | None) -> dict:
        e = self.get(eid)
        if not e:
            raise KeyError(eid)
        e["receipt"] = key
        self.b.put_expense(e)
        self._all = None
        return e

    def list(self, month: str | None = None, q: str = "", category: str = "", start: str = "", end: str = "") -> list[dict]:
        q = q.lower()
        return [e for e in self.all()
                if (not month or e["date"][:7] == month) and (not start or e["date"] >= start)
                and (not end or e["date"] <= end) and (not category or e["category"] == category)
                and (not q or q in e["vendor"].lower() or q in e["note"].lower())]

    # ── budgets and settings ──
    def budgets(self) -> dict[str, float]:
        return self.b.kv_get("budgets") or {}

    def set_budgets(self, values: dict):
        b = self.budgets()
        for cat, amount in values.items():
            if cat not in CATEGORIES:
                raise ValueError(f"unknown category {cat!r}")
            amount = num(amount or 0)
            if amount > 0:
                b[cat] = amount
            else:
                b.pop(cat, None)
        self.b.kv_put("budgets", b)

    def settings(self) -> dict[str, str]:
        return {**self.SETTINGS, **(self.b.kv_get("settings") or {})}

    def set_settings(self, values: dict):
        s = self.settings()
        for k, v in values.items():
            if k not in self.SETTINGS:
                raise ValueError(f"unknown setting {k!r}")
            s[k] = "1" if v in (True, 1, "1", "true") else "0"
        self.b.kv_put("settings", s)

    # ── reports ──
    def total(self, rows: list[dict]) -> float:
        return round(sum(e["inr"] for e in rows), 2)

    def month_total(self, month: str, category: str | None = None) -> float:
        return self.total(self.list(month, category=category or ""))

    def range_total(self, start: date, end: date) -> float:
        return self.total(self.list(start=start.isoformat(), end=end.isoformat()))

    @staticmethod
    def by(rows: list[dict], key) -> list[dict]:
        """Group rows → [{key, total, count}] largest first."""
        groups: dict[str, dict] = {}
        for e in rows:
            k = key(e)
            g = groups.setdefault(k.lower(), {"name": k, "total": 0.0, "count": 0})
            g["total"] = round(g["total"] + e["inr"], 2)
            g["count"] += 1
        return sorted(groups.values(), key=lambda g: -g["total"])

    def summary(self, month: str) -> dict:
        rows, prev = self.list(month), self.list(shift_month(month, -1))
        budgets = self.budgets()
        by_cat = {g["name"]: g for g in self.by(rows, lambda e: e["category"])}
        prev_cat = {g["name"]: g["total"] for g in self.by(prev, lambda e: e["category"])}
        cats = [{"category": c, "emoji": CATEGORIES[c], "total": by_cat.get(c, {}).get("total", 0),
                 "count": by_cat.get(c, {}).get("count", 0), "prev": prev_cat.get(c, 0), "budget": budgets.get(c)}
                for c in CATEGORIES if c in by_cat or c in budgets]
        cats.sort(key=lambda c: -c["total"])
        months = [shift_month(month, -i) for i in range(11, -1, -1)]
        return {
            "month": month, "today": today().isoformat(),
            "total": self.total(rows), "prev_total": self.total(prev), "count": len(rows),
            "categories": cats,
            "trend": [{"month": m, "total": self.month_total(m)} for m in months],
            "top_vendors": [{"vendor": g["name"], "total": g["total"], "count": g["count"]}
                            for g in self.by(rows, lambda e: e["vendor"])[:5]],
            "budget_total": round(sum(budgets.values()), 2) if budgets else None,
            "budget_spent": round(sum(c["total"] for c in cats if c["budget"]), 2),  # only budgeted categories
        }

    def week(self, start: date) -> dict:
        """Monday–Sunday week starting at `start`: total, vs the week before, categories, vendors, days."""
        end = start + timedelta(days=6)
        rows = self.list(start=start.isoformat(), end=end.isoformat())
        days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
        return {
            "start": start.isoformat(), "end": end.isoformat(),
            "total": self.total(rows),
            "prev_total": self.range_total(start - timedelta(days=7), start - timedelta(days=1)),
            "count": len(rows),
            "categories": [{"category": g["name"], "emoji": CATEGORIES.get(g["name"], "📦"), "total": g["total"],
                            "count": g["count"]} for g in self.by(rows, lambda e: e["category"])],
            "top_vendors": [{"vendor": g["name"], "total": g["total"], "count": g["count"]}
                            for g in self.by(rows, lambda e: e["vendor"])[:5]],
            "biggest": sorted(rows, key=lambda e: -e["inr"])[:3],
            "days": [{"date": d, "total": self.total([e for e in rows if e["date"] == d])} for d in days],
            "month_total": self.month_total(month_of(end)),
        }
