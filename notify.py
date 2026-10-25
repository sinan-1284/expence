"""
Discord notifications through a channel webhook (DISCORD_WEBHOOK_URL).

Posts: each new expense, budget alerts at 80% / 100%, subscriptions added on
their day, renewal reminders 3 days before, and a summary on the 1st of the month.
Without a webhook URL every post is quietly skipped.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from datetime import date, timedelta

from db import CATEGORIES, Store, inr, money, month_of, shift_month, today

BLUE, GREEN, AMBER, GRAY = 0x2A78D6, 0x0CA30C, 0xFAB219, 0x747472


def day(d: str) -> str:
    x = date.fromisoformat(d)
    return x.strftime("%-d %b") + ("" if x.year == today().year else x.strftime(" %Y"))


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
        """From the server's scheduler (every few minutes): due subscriptions, reminders, monthly summary."""
        for e in self.store.charge_due():
            self.expense_added(e)
        t = today()
        soon = (t + timedelta(days=3)).isoformat()
        for s in self.store.subscriptions(active_only=True):
            if t.isoformat() < s["next_date"] <= soon and self.store.once(f"remind:{s['id']}:{s['next_date']}"):
                self.post([{"title": f"⏰ {s['vendor']} renews {day(s['next_date'])}", "color": AMBER,
                            "description": f"{money(s['amount'], s['currency'])} {s['cycle']} · "
                                           f"{CATEGORIES[s['category']]} {s['category']}\n"
                                           "It is added automatically on the day. Cancelled it? Pause it on the "
                                           "dashboard.", "url": f"{self.url}/#/subscriptions" if self.url else None}])
        last = shift_month(month_of(t), -1)
        if t.day == 1 and self.store.once(f"monthly:{last}"):
            self.post([self.report_embed(last, title=f"📊 {month_name(last)} summary")])

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
        icon = "🔁" if e.get("source") == "subscription" else "💸"
        return {"title": f"{icon} {amount} · {e['vendor']}", "color": GREEN, "fields": fields,
                "footer": {"text": f"#{e['id']}" + (" · 📎 receipt" if e.get("receipt") else "")},
                "url": f"{self.url}/#/expenses" if self.url else None}

    def report_embed(self, month: str, title: str | None = None) -> dict:
        s = self.store.summary(month)
        delta = s["total"] - s["prev_total"]
        prev_name = month_name(shift_month(month, -1)).split()[0]
        vs = (f"{'▲' if delta > 0 else '▼'} {inr(abs(delta))} ({round(100 * delta / s['prev_total']):+d}%) vs {prev_name}"
              if s["prev_total"] else f"nothing spent in {prev_name}")
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
        if s["upcoming"]:
            fields.append({"name": "Coming up", "inline": True, "value": "\n".join(
                f"{u['vendor']} · {money(u['amount'], u['currency'])} on {day(u['next_date'])}" for u in s["upcoming"][:6])})
        return {"title": title or f"📊 {month_name(month)}", "color": BLUE, "fields": fields,
                "description": f"## {inr(s['total'])}\n{s['count']} expense{'s' * (s['count'] != 1)} · {vs}\n\n"
                               + ("\n".join(lines) or "No expenses."),
                "url": f"{self.url}/#/overview" if self.url else None}
