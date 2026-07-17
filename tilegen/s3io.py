"""Thin S3 wrapper: bucket bootstrap, listing, uploads with retry.

All keys passed in/out of this class are *relative* to the configured
prefix, so the rest of the code never has to think about it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .utils import retry

log = logging.getLogger("tilegen.s3")


class S3Store:
    def __init__(self, bucket: str, region: str = "us-east-1", prefix: str = ""):
        self.bucket = bucket
        self.region = region
        self.prefix = prefix.strip("/")
        self.client = boto3.client(
            "s3",
            region_name=region,
            config=Config(retries={"max_attempts": 5, "mode": "adaptive"},
                          max_pool_connections=32),
        )

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def ensure_bucket(self) -> bool:
        """Create the bucket if it does not exist. Returns True if created."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return False
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in ("404", "NoSuchBucket"):
                raise
        kwargs = {}
        if self.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        self.client.create_bucket(Bucket=self.bucket, **kwargs)
        self.client.put_public_access_block(
            Bucket=self.bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
            },
        )
        log.info("created bucket s3://%s (%s)", self.bucket, self.region)
        return True

    def list_keys(self, prefix: str):
        """Yield keys (relative to the store prefix) under a prefix."""
        full = self._full(prefix)
        strip = len(self._full("")) if self.prefix else 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            for obj in page.get("Contents", []):
                yield obj["Key"][strip:]

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._full(key))
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    @retry(times=4, delay=3.0, exceptions=(ClientError, ConnectionError))
    def upload(self, local: Path, key: str, metadata: dict | None = None) -> None:
        extra = {"ContentType": "image/tiff"}
        if metadata:
            extra["Metadata"] = {k: str(v) for k, v in metadata.items()}
        self.client.upload_file(str(local), self.bucket, self._full(key), ExtraArgs=extra)

    def get_json(self, key: str):
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._full(key))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise
        return json.loads(resp["Body"].read())

    def put_json(self, key: str, obj) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=self._full(key),
            Body=json.dumps(obj, indent=2, default=str).encode(),
            ContentType="application/json",
        )
