from functools import lru_cache

import boto3
from botocore.client import Config

from acestor_web.config import get_settings


class S3Client:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region: str,
        access_key: str,
        secret_key: str,
        use_path_style: bool,
    ) -> None:
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if use_path_style else "auto"},
            ),
        )

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except self._client.exceptions.ClientError:
            self._client.create_bucket(Bucket=self.bucket)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        tags: dict[str, str] | None = None,
    ) -> None:
        extra: dict = {"ContentType": content_type}
        if tags:
            extra["Tagging"] = "&".join(f"{k}={v}" for k, v in tags.items())
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def get_bytes(self, key: str) -> bytes:
        obj = self._client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def list_prefix(self, prefix: str) -> list[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    def delete_prefix(self, prefix: str) -> int:
        keys = self.list_prefix(prefix)
        if not keys:
            return 0
        count = 0
        for i in range(0, len(keys), 1000):
            batch = keys[i : i + 1000]
            self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
            count += len(batch)
        return count

    def set_tag(self, key: str, tags: dict[str, str]) -> None:
        self._client.put_object_tagging(
            Bucket=self.bucket,
            Key=key,
            Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]},
        )

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )


@lru_cache
def get_s3_client() -> S3Client:
    s = get_settings()
    client = S3Client(
        bucket=s.s3_bucket,
        endpoint_url=s.s3_endpoint_url,
        region=s.s3_region,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key,
        use_path_style=s.s3_use_path_style,
    )
    client.ensure_bucket()
    return client
