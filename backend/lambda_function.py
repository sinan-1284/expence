"""
AWS Lambda entry point: the API behind a Lambda Function URL, plus the daily EventBridge timer.

Environment (set by deploy/template.yaml): TABLE_NAME, RECEIPTS_BUCKET, DASHBOARD_PASSWORD,
TOKEN_SECRET, DISCORD_WEBHOOK_URL, PUBLIC_URL.
"""

import base64
import os
from urllib.parse import parse_qs

from api import App
from storage import DynamoBackend, S3Receipts

app = App(DynamoBackend(os.environ["TABLE_NAME"]), S3Receipts(os.environ["RECEIPTS_BUCKET"]),
          os.environ.get("DASHBOARD_PASSWORD"), os.environ["TOKEN_SECRET"], background_posts=False)


def handler(event, context):
    if event.get("source") == "aws.events" or event.get("detail-type") == "Scheduled Event":
        app.scheduled()
        return {"ok": True}
    http = event["requestContext"]["http"]
    body = event.get("body") or ""
    body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    qs = {k: v[0] for k, v in parse_qs(event.get("rawQueryString") or "").items()}
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    r = app.handle(http["method"], event.get("rawPath") or "/", qs, headers, body)
    text = r.ctype.startswith(("application/json", "text/"))
    return {"statusCode": r.status, "headers": {"Content-Type": r.ctype, "Cache-Control": "no-store", **r.headers},
            "body": r.body.decode() if text else base64.b64encode(r.body).decode(), "isBase64Encoded": not text}
