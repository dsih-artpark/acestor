from fastapi.testclient import TestClient

from acestor_web.db import get_db
from acestor_web.main import app
from acestor_web.models import AuditLog


def _client(db_session):
    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


HEADERS = {"X-Dev-User": "dev@example.com"}


def test_create_and_list(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/presets",
        json={"name": "p1", "yaml_text": "a: 1\n"},
        headers=HEADERS,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "p1"
    assert body["archived"] is False

    r = client.get("/api/presets", headers=HEADERS)
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_duplicate_name_conflicts(db_session):
    client = _client(db_session)
    client.post("/api/presets", json={"name": "p", "yaml_text": "x\n"}, headers=HEADERS)
    r = client.post(
        "/api/presets", json={"name": "p", "yaml_text": "y\n"}, headers=HEADERS
    )
    assert r.status_code == 409


def test_update_writes_audit(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/presets", json={"name": "p", "yaml_text": "x\n"}, headers=HEADERS
    )
    pid = r.json()["id"]
    r = client.patch(
        f"/api/presets/{pid}", json={"description": "new"}, headers=HEADERS
    )
    assert r.status_code == 200
    assert r.json()["description"] == "new"

    audits = db_session.query(AuditLog).filter_by(action="preset.update").all()
    assert len(audits) == 1
    assert audits[0].before_json["description"] == ""
    assert audits[0].after_json["description"] == "new"


def test_archive_hides_from_default_list(db_session):
    client = _client(db_session)
    r = client.post(
        "/api/presets", json={"name": "p", "yaml_text": "x\n"}, headers=HEADERS
    )
    pid = r.json()["id"]

    r = client.delete(f"/api/presets/{pid}", headers=HEADERS)
    assert r.status_code == 204

    r = client.get("/api/presets", headers=HEADERS)
    assert r.json() == []

    r = client.get("/api/presets?include_archived=true", headers=HEADERS)
    assert len(r.json()) == 1
    assert r.json()[0]["archived"] is True


def test_404_on_missing(db_session):
    client = _client(db_session)
    r = client.get("/api/presets/00000000-0000-0000-0000-000000000000", headers=HEADERS)
    assert r.status_code == 404


def test_update_to_duplicate_name_conflicts(db_session):
    client = _client(db_session)
    client.post("/api/presets", json={"name": "a", "yaml_text": "x\n"}, headers=HEADERS)
    r = client.post(
        "/api/presets", json={"name": "b", "yaml_text": "y\n"}, headers=HEADERS
    )
    bid = r.json()["id"]
    r = client.patch(f"/api/presets/{bid}", json={"name": "a"}, headers=HEADERS)
    assert r.status_code == 409
