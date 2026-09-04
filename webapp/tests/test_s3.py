import os

import pytest

from acestor_web.storage import S3Client


@pytest.fixture
def s3_client() -> S3Client:
    bucket = os.environ.get("S3_BUCKET", "acestor-test")
    c = S3Client(
        bucket=bucket,
        endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000"),
        region="us-east-1",
        access_key="minioadmin",
        secret_key="minioadmin",
        use_path_style=True,
    )
    c.ensure_bucket()
    c.delete_prefix("test/")
    return c


def test_put_and_get(s3_client):
    s3_client.put_bytes("test/hello.txt", b"hi", content_type="text/plain")
    assert s3_client.get_bytes("test/hello.txt") == b"hi"


def test_list_and_delete_prefix(s3_client):
    for i in range(3):
        s3_client.put_bytes(f"test/list/{i}.bin", b"x")
    keys = s3_client.list_prefix("test/list/")
    assert len(keys) == 3
    n = s3_client.delete_prefix("test/list/")
    assert n == 3
    assert s3_client.list_prefix("test/list/") == []


def test_set_tag(s3_client):
    s3_client.put_bytes("test/tagged.bin", b"x")
    s3_client.set_tag("test/tagged.bin", {"pinned": "true"})
    # No public read-tags helper; smoke via boto directly is fine but skipped.
