# expenses

DDLC AI expense tracker: a web dashboard at **https://expense.ddlc.ai** that posts to a Discord channel through a webhook.

```
browser ──https──▶ expense.ddlc.ai ──▶ server.py :9200 ──▶ data/expenses.db (SQLite) + data/receipts/
                                            │
                                            └──webhook──▶ Discord #expenses
```

Standard-library Python 3.9+, no pip installs. One shared password.

## Dashboard

| Section | What it does |
|---|---|
| Overview | month total, change vs last month, per-day spend, this week, budget use; 12-month bar chart (click a month); spending by category with budget lines; this week by day; top vendors; latest expenses |
| Expenses | add (amount, INR / USD, vendor, category, date, note, receipt photo or PDF), search, filter, edit, delete, CSV for the month |
| Budgets | monthly limit per category with on track / near limit / over status |
| Reports & export | weekly / monthly Discord reports on or off, post a report now, test message; CSV for a month, the Indian financial year (Apr–Mar), everything or a date range |

Leaving the category on **Auto** picks the category last used for that vendor, or guesses from the name (Hetzner → Cloud & hosting, Swiggy → Food, …). USD amounts are converted to rupees at that day's ECB rate (frankfurter.dev); totals are always in ₹.

## Discord

Messages posted to the webhook's channel:

| When | Message |
|---|---|
| An expense is added | amount, vendor, category, date, month so far |
| A category passes 80% / 100% of its budget | 🟠 / 🔴 alert on that expense, once a month each |
| Monday, 9:00 IST (weekly report on) | 🗓️ last week: total, change vs the week before, categories, spend per day, top vendors, biggest expenses |
| 1st of the month, 9:00 IST (monthly report on) | 📊 last month: total, change, categories vs budgets, top vendors |

Create the webhook in Discord: channel → Edit channel → Integrations → Webhooks → New webhook → Copy webhook URL. Put it in `.env` as `DISCORD_WEBHOOK_URL`.

## Run locally

```bash
cp .env.example .env && chmod 600 .env       # password, TOKEN_SECRET, webhook URL
set -a; . ./.env; set +a
python3 server.py                            # http://localhost:9200/
```

## Deploy (AWS)

It is one long-running Python process that keeps its data on local disk (`data/`), so it needs a
machine with a persistent disk: an **EC2** instance or **Lightsail** (the smallest size is plenty).
Amplify Hosting and App Runner do not fit: they serve static sites / restart containers without a
disk, so the SQLite file and receipts would be lost.

On an Ubuntu instance:

```bash
git clone https://github.com/sinan-1284/expence.git ~/expenses
cd ~/expenses
cp .env.example .env && chmod 600 .env       # set the values
mkdir -p ~/.config/systemd/user
cp expenses.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now expenses
sudo loginctl enable-linger $USER            # start at boot without a login
curl -s localhost:9200/ | head -3
```

The service listens on 127.0.0.1:9200 only. Put HTTPS in front of it for expense.ddlc.ai, either:

- **Cloudflare Tunnel** (`ddlc.ai` DNS is on Cloudflare; no inbound ports to open):

  ```bash
  cloudflared tunnel login
  cloudflared tunnel create ddlc-expenses
  cloudflared tunnel route dns ddlc-expenses expense.ddlc.ai
  # ~/.cloudflared/config.yml → ingress: hostname expense.ddlc.ai, service http://localhost:9200
  ```

- or **Caddy** (open ports 80/443 in the security group, point an A record at the instance's Elastic IP):

  ```
  expense.ddlc.ai {
      reverse_proxy 127.0.0.1:9200
  }
  ```

## Data

Everything is in `data/` (git-ignored):

- `expenses.db`: SQLite, the expenses, budgets, report settings and saved exchange rates
- `receipts/`: receipt files, up to 10 MB each
- `backups/expenses-YYYY-MM-DD.db`: a copy every day, last 30 kept

To move to another server, copy `data/` and `.env`.

## API

All under `/api/`, with `Authorization: Bearer <token>` from `POST /api/login` (or `?token=` for downloads). See the top of `server.py` for the list.
