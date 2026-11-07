"""The M1.4 flow end to end over HTTP, against the test database only: real registration and
login, then every projects capability in the order a team would use them."""

import hashlib
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import get_session_factory
from app.modules.projects import service
from app.modules.projects.models import ApiKey, Project, ProjectMember

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

AUTH = "/api/v1/auth"
PROJECTS = "/api/v1/projects"
PASSWORD = "correct horse battery staple"


def _sign_up(client: TestClient, email: str) -> dict[str, str]:
    registered = client.post(f"{AUTH}/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text
    login = client.post(f"{AUTH}/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _authenticate(key: str) -> Any:
    with get_session_factory()() as session:
        return service.authenticate_api_key(session, key)


def _count(model: type, project_id: uuid.UUID) -> int:
    with get_session_factory()() as session:
        return session.scalar(select(func.count()).select_from(model).where(model.project_id == project_id)) or 0


def test_project_lifecycle(client: TestClient) -> None:
    owner = _sign_up(client, "owner@example.com")
    admin = _sign_up(client, "admin@example.com")
    member = _sign_up(client, "member@example.com")
    me = client.get(f"{AUTH}/me", headers=admin).json()

    # Create a project; the creator is its Owner.
    created = client.post(PROJECTS, json={"name": "Search assistant"}, headers=owner)
    assert created.status_code == 201
    project_id = created.json()["id"]
    assert created.json()["role"] == "owner"
    url = f"{PROJECTS}/{project_id}"

    # Add an Admin and a Member.
    assert client.post(f"{url}/members", json={"email": "admin@example.com", "role": "admin"}, headers=owner).status_code == 201
    assert client.post(f"{url}/members", json={"email": "member@example.com", "role": "member"}, headers=admin).status_code == 201
    members = client.get(f"{url}/members", headers=member).json()
    assert [(m["email"], m["role"]) for m in members] == [
        ("owner@example.com", "owner"),
        ("admin@example.com", "admin"),
        ("member@example.com", "member"),
    ]

    # Update retention (Admin); every member reads the effective values.
    updated = client.patch(f"{url}/retention", json={"traces_days": 7, "prediction_logs_days": 14}, headers=admin)
    assert updated.status_code == 200
    retention = client.get(f"{url}/retention", headers=member).json()
    assert retention["effective"]["traces_days"] == 7
    assert retention["effective"]["evaluation_results_days"] == retention["defaults"]["evaluation_results_days"]
    assert client.patch(f"{url}/retention", json={"traces_days": 1}, headers=member).status_code == 403

    # Create an API key (Admin): shown once, stored only as its SHA-256 digest.
    key_response = client.post(f"{url}/api-keys", json={"name": "Ingestion"}, headers=admin)
    assert key_response.status_code == 201
    assert key_response.headers["Cache-Control"] == "no-store"
    key, key_id = key_response.json()["key"], key_response.json()["id"]
    with get_session_factory()() as session:
        assert session.get_one(ApiKey, uuid.UUID(key_id)).key_hash == hashlib.sha256(key.encode()).hexdigest()
    listing = client.get(f"{url}/api-keys", headers=owner)
    assert key not in listing.text and [k["id"] for k in listing.json()] == [key_id]
    assert client.get(f"{url}/api-keys", headers=member).status_code == 403

    # The key authenticates its project through the service (no HTTP ingestion route until M2).
    principal = _authenticate(key)
    assert principal is not None and str(principal.project_id) == project_id

    # Revoke it: it never authenticates again.
    revoked = client.post(f"{url}/api-keys/{key_id}/revoke", headers=owner)
    assert revoked.status_code == 200 and revoked.json()["is_active"] is False
    assert _authenticate(key) is None

    # Transfer ownership to the Admin; the previous Owner becomes an Admin.
    transfer = client.post(f"{url}/ownership-transfer", json={"user_id": me["id"]}, headers=owner)
    assert transfer.status_code == 200
    assert transfer.json()["new_owner"]["role"] == "owner"
    assert transfer.json()["previous_owner"]["role"] == "admin"
    assert client.get(url, headers=admin).json()["role"] == "owner"
    assert client.request("DELETE", url, json={"confirm_name": "Search assistant"}, headers=owner).status_code == 403

    # The new Owner deletes the project; memberships and keys go with it.
    deleted = client.request("DELETE", url, json={"confirm_name": "Search assistant"}, headers=admin)
    assert deleted.status_code == 204
    assert client.get(url, headers=admin).status_code == 404
    assert client.get(PROJECTS, headers=member).json() == []
    pid = uuid.UUID(project_id)
    with get_session_factory()() as session:
        assert session.get(Project, pid) is None
    assert _count(ProjectMember, pid) == 0
    assert _count(ApiKey, pid) == 0
