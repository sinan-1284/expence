"""
Discord notifications through a channel webhook (DISCORD_WEBHOOK_URL).

Posts: each new expense, budget alerts at 80% / 100%, a weekly report on Monday
morning and a monthly report on the 1st (each can be turned off on the dashboard).
Without a webhook URL every post is quietly skipped.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from datetime import date, datetime, timedelta

from db import CATEGORIES, TZ, Store, inr, money, month_of, shift_month, today, week_start

BLUE, GREEN, AMBER, GRAY = 0x2A78D6, 0x0CA30C, 0xFAB219, 0x747472
REPORT_HOUR = 9  # scheduled reports go out from 09:00 IST


def day(d: str) -> str:
    x = date.fromisoformat(d)
    return x.strftime("%-d %b") + ("" if x.year == today().year else x.strftime(" %Y"))


def week_name(start: str) -> str:
    """'19 – 25 Oct' / '28 Sep – 4 Oct'."""
    a, b = date.fromisoformat(start), date.fromisoformat(start) + timedelta(days=6)
    return f"{a.day} – {day(b.isoformat())}" if a.month == b.month else f"{day(a.isoformat())} – {day(b.isoformat())}"


def change(total: float, prev: float, label: str) -> str:
    if not prev:
        return f"nothing spent {label}"
    d = total - prev
    return f"{'▲' if d > 0 else '▼' if d < 0 else '■'} {inr(abs(d))} ({round(100 * d / prev):+d}%) vs {label}"


def month_name(m: str) -> str:
    return date.fromisoformat(m + "-01").strftime("%B %Y")


def strip_none(x):
    """Drop null fields (an embed without a footer or url) before sending."""
    if isinstance(x, dict):
        return {k: strip_none(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [strip_none(v) for v in x]
    return x


class Notifier:
    def __init__(self, store: Store):
        self.store = store
        self.webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        self.url = os.environ.get("PUBLIC_URL", "").rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.webhook)

    def post(self, embeds: list[dict], content: str = "", wait: bool = False) -> str | None:
        """Send to the webhook. In the background unless wait=True; returns an error text (wait=True) or None."""
        if not self.webhook:
            return "DISCORD_WEBHOOK_URL is not set"
        body = json.dumps(strip_none({"username": "DDLC Expenses", "content": content, "embeds": embeds,
                                      "allowed_mentions": {"parse": []}})).encode()

        def send():
            req = urllib.request.Request(self.webhook, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "ddlc-expenses"})
            try:
                urllib.request.urlopen(req, timeout=15).close()
            except Exception as e:
                print("discord webhook failed:", e, file=sys.stderr)
                return str(getattr(e, "reason", e))
            return None

        if wait:
            return send()
        threading.Thread(target=send, daemon=True).start()
        return None

    # ── events ──
    def expense_added(self, e: dict):
        embed = self.expense_embed(e)
        if alerts := self.budget_alerts(e):
            embed["description"] = "\n".join(alerts)
            embed["color"] = AMBER
        self.post([embed])

    def daily(self):
        """From the server's scheduler (every few minutes): weekly report on Monday, monthly on the 1st."""
        now = datetime.now(TZ)
        if now.hour < REPORT_HOUR:
            return
        t, settings = now.date(), self.store.settings()
        last_week = week_start(t) - timedelta(days=7)
        if t.weekday() == 0 and settings["weekly_report"] == "1" and self.store.once(f"weekly:{last_week}"):
            self.post([self.week_embed(last_week)])
        last_month = shift_month(month_of(t), -1)
        if t.day == 1 and settings["monthly_report"] == "1" and self.store.once(f"monthly:{last_month}"):
            self.post([self.report_embed(last_month, title=f"📊 {month_name(last_month)} summary")])

    def budget_alerts(self, e: dict) -> list[str]:
        """Text for a budget this expense pushed past 80% or 100% (each once a month)."""
        month = month_of(e["date"])
        b = self.store.budgets().get(e["category"])
        if not b or month != month_of(today()):
            return []
        spent = self.store.month_total(month, e["category"])
        pct = round(100 * spent / b)
        if spent >= b and self.store.once(f"budget:{month}:{e['category']}:100"):
            self.store.once(f"budget:{month}:{e['category']}:80")
            return [f"🔴 **{e['category']}** is over budget: {inr(spent)} of {inr(b)} ({pct}%)"]
        if spent >= 0.8 * b and self.store.once(f"budget:{month}:{e['category']}:80"):
            return [f"🟠 **{e['category']}** is at {pct}% of its {inr(b)} budget ({inr(spent)} spent)"]
        return []

    # ── embeds ──
    def expense_embed(self, e: dict) -> dict:
        amount = money(e["amount"], e["currency"]) + (f" ({inr(e['inr'])})" if e["currency"] != "INR" else "")
        month = month_of(e["date"])
        fields = [{"name": "Category", "value": f"{CATEGORIES[e['category']]} {e['category']}", "inline": True},
                  {"name": "Date", "value": day(e["date"]), "inline": True},
                  {"name": f"{month_name(month).split()[0]} so far", "value": inr(self.store.month_total(month)),
                   "inline": True}]
        if e["note"]:
            fields.append({"name": "Note", "value": e["note"][:1000]})
        return {"title": f"💸 {amount} · {e['vendor']}", "color": GREEN, "fields": fields,
                "footer": {"text": f"#{e['id']}" + (" · 📎 receipt" if e.get("receipt") else "")},
                "url": f"{self.url}/#/expenses" if self.url else None}

    def report_embed(self, month: str, title: str | None = None) -> dict:
        s = self.store.summary(month)
        vs = change(s["total"], s["prev_total"], month_name(shift_month(month, -1)).split()[0])
        lines = []
        for c in s["categories"]:
            line = f"{c['emoji']} {c['category']} · **{inr(c['total'])}**"
            if c["budget"]:
                pct = round(100 * c["total"] / c["budget"])
                line += f" of {inr(c['budget'])} {'🔴' if pct >= 100 else '🟠' if pct >= 80 else '🟢'} {pct}%"
            lines.append(line)
        fields = []
        if s["top_vendors"]:
            fields.append({"name": "Top vendors", "inline": True,
                           "value": "\n".join(f"{v['vendor']} · {inr(v['total'])}" for v in s["top_vendors"])})
        return {"title": title or f"📊 {month_name(month)}", "color": BLUE, "fields": fields,
                "description": f"## {inr(s['total'])}\n{s['count']} expense{'s' * (s['count'] != 1)} · {vs}\n\n"
                               + ("\n".join(lines) or "No expenses."),
                "url": f"{self.url}/#/overview?m={month}" if self.url else None}

    def week_embed(self, start: date) -> dict:
        w = self.store.week(start)
        vs = change(w["total"], w["prev_total"], "the week before")
        lines = [f"{c['emoji']} {c['category']} · **{inr(c['total'])}**" for c in w["categories"]]
        fields = [{"name": "By day", "inline": True, "value": "\n".join(
            f"{date.fromisoformat(d['date']).strftime('%a')} · {inr(d['total']) if d['total'] else '—'}" for d in w["days"])}]
        if w["top_vendors"]:
            fields.append({"name": "Top vendors", "inline": True,
                           "value": "\n".join(f"{v['vendor']} · {inr(v['total'])}" for v in w["top_vendors"])})
        if w["biggest"]:
            fields.append({"name": "Biggest", "inline": True, "value": "\n".join(
                f"{e['vendor']} · {money(e['amount'], e['currency'])} ({day(e['date'])})" for e in w["biggest"])})
        month = month_of(w["end"])
        return {"title": f"🗓️ Week {week_name(w['start'])}", "color": BLUE, "fields": fields,
                "description": f"## {inr(w['total'])}\n{w['count']} expense{'s' * (w['count'] != 1)} · {vs}\n\n"
                               + ("\n".join(lines) or "No expenses this week."),
                "footer": {"text": f"{month_name(month).split()[0]} so far: {inr(w['month_total'])}"},
                "url": f"{self.url}/#/overview?m={month}" if self.url else None}
