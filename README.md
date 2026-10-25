# expence

DDLC AI expense tracker: a web dashboard at **https://expence.nexbotlabs.in** that posts to a Discord channel through a webhook.

```
browser ──▶ expence.nexbotlabs.in   GitHub Pages: web/index.html + web/config.js
   │
   └──fetch──▶ Lambda Function URL   backend/ (Python 3.12) ──▶ DynamoDB  expenses, budgets, settings
                     ▲                    │            └──────▶ S3        receipts (browser uploads with a presigned URL)
   EventBridge, daily 09:05 IST ┘         └──webhook──▶ Discord #expenses
```

The same page is also on AWS Amplify Hosting (expence.ddlc.ai, `amplify.yml`). Python standard library plus boto3, which Lambda provides; nothing to install.

## Dashboard

| Section | What it does |
|---|---|
| Overview | month total, change vs last month, per-day spend, this week, budget use; 12-month bar chart (click a month); spending by category with budget lines; this week by day; top vendors; latest expenses |
| Expenses | add (amount, INR / USD, vendor, category, date, note, receipt photo or PDF), search, filter, edit, delete, CSV for the month |
| Budgets | monthly limit per category with on track / near limit / over status |
| Reports & export | weekly / monthly Discord reports on or off, post a report now, test message; CSV for a month, the Indian financial year (Apr–Mar), everything or a date range |

Leaving the category on **Auto** picks the category last used for that vendor, or guesses from the name (Hetzner → Cloud & hosting, Swiggy → Food, …). USD amounts are converted to rupees at that day's ECB rate (frankfurter.dev); totals are always in ₹.

## Discord

| When | Message |
|---|---|
| An expense is added | amount, vendor, category, date, month so far |
| A category passes 80% / 100% of its budget | 🟠 / 🔴 alert on that expense, once a month each |
| Monday, 9:05 IST (weekly report on) | 🗓️ last week: total, change vs the week before, categories, spend per day, top vendors, biggest expenses |
| 1st of the month, 9:05 IST (monthly report on) | 📊 last month: total, change, categories vs budgets, top vendors |

Webhook: Discord channel → Edit channel → Integrations → Webhooks → New webhook → Copy webhook URL.

## Files

| Path | |
|---|---|
| `web/index.html` | the whole dashboard (one page) |
| `web/config.js` | the API address, written by `deploy/deploy.sh` |
| `backend/api.py` | routes and login |
| `backend/core.py` | expenses, totals, reports, budgets, currency |
| `backend/notify.py` | Discord messages |
| `backend/storage.py` | DynamoDB + S3, or a JSON file + folder when local |
| `backend/lambda_function.py` | Lambda entry point (API + daily timer) |
| `server.py` | run everything on this machine |
| `deploy/template.yaml`, `deploy/deploy.sh` | the AWS side (CloudFormation) |
| `.github/workflows/pages.yml` | publishes `web/` to GitHub Pages on push |

## Deploy

**API (AWS, region ap-south-1):**

```bash
cp .env.example .env && chmod 600 .env      # password, TOKEN_SECRET, webhook, PUBLIC_URL
aws login
deploy/deploy.sh                            # creates / updates stack "ddlc-expenses", uploads backend/
git add web/config.js && git commit -m "API URL" && git push    # first deploy only
```

Re-run `deploy/deploy.sh` after changing anything in `backend/` or `.env`. The DynamoDB table and the receipts bucket are kept if the stack is deleted. DynamoDB point-in-time recovery is on (any second in the last 35 days).

**Page (GitHub Pages):** pushing `web/` to `main` publishes it. Repo Settings → Pages: source *GitHub Actions*, custom domain `expence.nexbotlabs.in`, Enforce HTTPS.

**DNS (GoDaddy, nexbotlabs.in):** `CNAME  expence  →  sinan-1284.github.io`

## Run locally

```bash
set -a; . ./.env; set +a
python3 server.py                            # http://localhost:9200/  (data in data/)
```

## Cost

Within the AWS free tier for one person's expenses: Lambda and DynamoDB on-demand charge per request, S3 per GB stored. GitHub Pages is free for a public repo.
