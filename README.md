# expenses

DDLC AI expense tracker: a web dashboard at **https://expense.ddlc.ai** that posts to a Discord channel through a webhook.

```
browser ──https──▶ expense.ddlc.ai ══ Cloudflare Tunnel ══▶ server.py :9200 ──▶ data/expenses.db (SQLite)
                                                                │
                                                                └──webhook──▶ Discord #expenses
```

Standard-library Python 3.9+, no pip installs. One shared password.

## Dashboard

| Section | What it does |
|---|---|
| Overview | month total, change vs last month, per-day spend, recurring cost, budget use; 12-month bar chart (click a month); spending by category with budget lines; renewals due in 30 days; top vendors; latest expenses |
| Expenses | add (amount, INR / USD, vendor, category, date, note, receipt photo or PDF), search, filter, edit, delete, CSV for the month |
| Subscriptions | recurring bills (monthly / yearly). Each is added as an expense on its day; pause, edit, delete |
| Budgets | monthly limit per category with on track / near limit / over status |
| Discord & export | webhook status, test message, post a month's report now; CSV for a month, the Indian financial year (Apr–Mar), everything or a date range |

Leaving the category on **Auto** picks the category last used for that vendor, or guesses from the name (Hetzner → Cloud & hosting, Swiggy → Food, …). USD amounts are converted to rupees at that day's ECB rate (frankfurter.dev); totals are always in ₹.

## Discord

Messages posted to the webhook's channel:

| When | Message |
|---|---|
| An expense is added | amount, vendor, category, date, month so far |
| A category passes 80% / 100% of its budget | 🟠 / 🔴 alert on that expense, once a month each |
| A subscription's day comes | 🔁 the bill, added as an expense |
| 3 days before a renewal | ⏰ reminder |
| 1st of the month | 📊 last month's summary: total, change, categories vs budgets, top vendors, upcoming renewals |

Create the webhook in Discord: channel → Edit channel → Integrations → Webhooks → New webhook → Copy webhook URL. Put it in `.env` as `DISCORD_WEBHOOK_URL`.

## Install (server)

```bash
git clone git@github.com:AI-DDLC/expenses.git ~/expenses    # or copy the folder
cd ~/expenses
cp .env.example .env && chmod 600 .env                       # set the password, TOKEN_SECRET, webhook
mkdir -p ~/.config/systemd/user
cp expenses.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now expenses
sudo loginctl enable-linger $USER                            # start at boot without a login
curl -s localhost:9200/ | head -3
```

The service listens on 127.0.0.1 only. It is published with a Cloudflare Tunnel (`ddlc.ai` DNS is on Cloudflare):

```bash
cloudflared tunnel login                                     # once: browser approval for ddlc.ai
cloudflared tunnel create ddlc-expenses
cloudflared tunnel route dns ddlc-expenses expense.ddlc.ai
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: ddlc-expenses
credentials-file: /home/<user>/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: expense.ddlc.ai
    service: http://localhost:9200
  - service: http_status:404
```

and run it as a service (`cloudflared service install`, or a user unit running `cloudflared --no-autoupdate tunnel run ddlc-expenses`).

## Data

Everything is in `data/` (git-ignored):

- `expenses.db`: SQLite, the expenses, subscriptions, budgets and saved exchange rates
- `receipts/`: receipt files, up to 10 MB each
- `backups/expenses-YYYY-MM-DD.db`: a copy every day, last 30 kept

To move to another server, copy `data/` and `.env`.

## API

All under `/api/`, with `Authorization: Bearer <token>` from `POST /api/login` (or `?token=` for downloads). See the top of `server.py` for the list.
