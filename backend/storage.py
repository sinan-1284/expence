"""
Where expenses and receipts are kept.

  AWS (Lambda):  DynamoDB table (TABLE_NAME) + S3 bucket for receipts (RECEIPTS_BUCKET)
  Local:         data/store.json + data/receipts/

DynamoDB items (pk, sk):
  EXP  / 0000000012   {"doc": <expense JSON>}
  META / counter      {"n": last expense id}
  KV   / budgets | settings | fx       {"doc": <JSON>}
  ONCE / weekly:2026-10-19 …          "already sent" markers
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

RECEIPT_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
                 "application/pdf": ".pdf"}
CONTENT_TYPES = {v: k for k, v in RECEIPT_TYPES.items()} | {".jpeg": "image/jpeg"}
MAX_RECEIPT = 10 * 1024 * 1024


# ── expenses / settings ──

class FileBackend:
    """Everything in one JSON file. For running on a laptop; one process only."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.data = json.loads(path.read_text()) if path.exists() else {"next_id": 0, "expenses": {}, "kv": {}, "once": {}}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    def expenses(self) -> list[dict]:
        with self.lock:
            return [dict(e) for e in self.data["expenses"].values()]

    def get_expense(self, eid: int) -> dict | None:
        with self.lock:
            e = self.data["expenses"].get(str(eid))
            return dict(e) if e else None

    def put_expense(self, doc: dict):
        with self.lock:
            self.data["expenses"][str(doc["id"])] = doc
            self._save()

    def delete_expense(self, eid: int):
        with self.lock:
            self.data["expenses"].pop(str(eid), None)
            self._save()

    def next_id(self) -> int:
        with self.lock:
            self.data["next_id"] += 1
            self._save()
            return self.data["next_id"]

    def kv_get(self, key: str):
        with self.lock:
            return self.data["kv"].get(key)

    def kv_put(self, key: str, value):
        with self.lock:
            self.data["kv"][key] = value
            self._save()

    def once(self, key: str) -> bool:
        with self.lock:
            if key in self.data["once"]:
                return False
            self.data["once"][key] = True
            self._save()
            return True


class DynamoBackend:
    def __init__(self, table_name: str):
        import boto3
        self.table = boto3.resource("dynamodb").Table(table_name)

    def expenses(self) -> list[dict]:
        out, kw = [], {"KeyConditionExpression": "pk = :p", "ExpressionAttributeValues": {":p": "EXP"}}
        while True:
            r = self.table.query(**kw)
            out += [json.loads(i["doc"]) for i in r["Items"]]
            if "LastEvaluatedKey" not in r:
                return out
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def get_expense(self, eid: int) -> dict | None:
        i = self.table.get_item(Key={"pk": "EXP", "sk": f"{eid:010d}"}).get("Item")
        return json.loads(i["doc"]) if i else None

    def put_expense(self, doc: dict):
        self.table.put_item(Item={"pk": "EXP", "sk": f"{doc['id']:010d}", "doc": json.dumps(doc, ensure_ascii=False)})

    def delete_expense(self, eid: int):
        self.table.delete_item(Key={"pk": "EXP", "sk": f"{eid:010d}"})

    def next_id(self) -> int:
        r = self.table.update_item(Key={"pk": "META", "sk": "counter"}, UpdateExpression="ADD n :one",
                                   ExpressionAttributeValues={":one": 1}, ReturnValues="UPDATED_NEW")
        return int(r["Attributes"]["n"])

    def kv_get(self, key: str):
        i = self.table.get_item(Key={"pk": "KV", "sk": key}).get("Item")
        return json.loads(i["doc"]) if i else None

    def kv_put(self, key: str, value):
        self.table.put_item(Item={"pk": "KV", "sk": key, "doc": json.dumps(value, ensure_ascii=False)})

    def once(self, key: str) -> bool:
        try:
            self.table.put_item(Item={"pk": "ONCE", "sk": key}, ConditionExpression="attribute_not_exists(pk)")
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False


# ── receipts ──
# The browser uploads a receipt straight to `upload_target(...)["url"]` with a PUT, then attaches its key.

class LocalReceipts:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def upload_target(self, key: str, ctype: str, token: str) -> dict:
        return {"url": f"/api/upload?key={key}&token={token}", "headers": {"Content-Type": ctype}}

    def save(self, key: str, data: bytes):
        (self.folder / Path(key).name).write_bytes(data)

    def exists(self, key: str) -> bool:
        return (self.folder / Path(key).name).exists()

    def read(self, key: str) -> bytes:
        return (self.folder / Path(key).name).read_bytes()

    def delete(self, key: str):
        (self.folder / Path(key).name).unlink(missing_ok=True)


class S3Receipts:
    def __init__(self, bucket: str):
        import boto3
        from botocore.config import Config
        region = os.environ.get("AWS_REGION", "ap-south-1")
        self.bucket = bucket
        self.s3 = boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com",
                               config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))

    def upload_target(self, key: str, ctype: str, token: str) -> dict:
        url = self.s3.generate_presigned_url("put_object", ExpiresIn=600,
                                             Params={"Bucket": self.bucket, "Key": key, "ContentType": ctype})
        return {"url": url, "headers": {"Content-Type": ctype}}

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def view_url(self, key: str, filename: str) -> str:
        return self.s3.generate_presigned_url("get_object", ExpiresIn=600, Params={
            "Bucket": self.bucket, "Key": key, "ResponseContentDisposition": f'inline; filename="{filename}"'})

    def delete(self, key: str):
        self.s3.delete_object(Bucket=self.bucket, Key=key)
