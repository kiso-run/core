"""Integration tests for projects, cron, and safety-rules APIs.

Exercises the full request → route → store → response cycle using an
ASGI-backed httpx AsyncClient with mocked LLM.

Requires ``--integration`` flag.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import AUTH_HEADER

pytestmark = pytest.mark.integration


# ── Projects API ──────────────────────────────────────────────


class TestProjectsAPI:
    async def test_create_and_list_project(self, kiso_client):
        # Create
        resp = await kiso_client.post(
            "/projects",
            json={"name": "test-proj", "description": "Integration test"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-proj"
        assert "id" in data
        project_id = data["id"]

        # List
        resp = await kiso_client.get(
            "/projects", params={"user": "testadmin"}, headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        projects = resp.json()["projects"]
        assert any(p["name"] == "test-proj" for p in projects)

    async def test_get_project_details(self, kiso_client):
        await kiso_client.post(
            "/projects", json={"name": "detail-proj"}, headers=AUTH_HEADER,
        )
        resp = await kiso_client.get(
            "/projects/detail-proj", headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["project"]["name"] == "detail-proj"
        assert "members" in data

    async def test_add_and_list_members(self, kiso_client):
        await kiso_client.post(
            "/projects", json={"name": "member-proj"}, headers=AUTH_HEADER,
        )
        # Add member
        resp = await kiso_client.post(
            "/projects/member-proj/members",
            json={"username": "bob", "role": "member"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["added"] is True

        # List members
        resp = await kiso_client.get(
            "/projects/member-proj/members", headers=AUTH_HEADER,
        )
        members = resp.json()["members"]
        usernames = [m["username"] for m in members]
        assert "bob" in usernames

    async def test_remove_member(self, kiso_client):
        await kiso_client.post(
            "/projects", json={"name": "rm-proj"}, headers=AUTH_HEADER,
        )
        await kiso_client.post(
            "/projects/rm-proj/members",
            json={"username": "carol", "role": "viewer"},
            headers=AUTH_HEADER,
        )
        resp = await kiso_client.delete(
            "/projects/rm-proj/members/carol", headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

        # Verify removed
        resp = await kiso_client.get(
            "/projects/rm-proj/members", headers=AUTH_HEADER,
        )
        usernames = [m["username"] for m in resp.json()["members"]]
        assert "carol" not in usernames

    async def test_bind_and_unbind_session(self, kiso_client):
        await kiso_client.post(
            "/projects", json={"name": "bind-proj"}, headers=AUTH_HEADER,
        )
        # Create session first
        await kiso_client.post(
            "/sessions",
            json={"session": "bind-sess", "user": "testadmin"},
            headers=AUTH_HEADER,
        )
        # Bind
        resp = await kiso_client.post(
            "/projects/bind-proj/bind/bind-sess", headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["bound"] is True

        # Unbind
        resp = await kiso_client.post(
            "/projects/bind-proj/unbind/bind-sess", headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["unbound"] is True

    async def test_project_not_found(self, kiso_client):
        resp = await kiso_client.get(
            "/projects/nonexistent", headers=AUTH_HEADER,
        )
        assert resp.status_code == 404

    async def test_duplicate_project(self, kiso_client):
        await kiso_client.post(
            "/projects", json={"name": "dup-proj"}, headers=AUTH_HEADER,
        )
        resp = await kiso_client.post(
            "/projects", json={"name": "dup-proj"}, headers=AUTH_HEADER,
        )
        assert resp.status_code == 409


# ── Cron API ──────────────────────────────────────────────────


class TestCronAPI:
    async def test_create_and_list(self, kiso_client):
        resp = await kiso_client.post(
            "/cron",
            json={"session": "cron-sess", "schedule": "0 9 * * *",
                  "prompt": "Daily backup"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["schedule"] == "0 9 * * *"
        job_id = data["id"]

        # List
        resp = await kiso_client.get("/cron", headers=AUTH_HEADER)
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert any(j["id"] == job_id for j in jobs)

    async def test_filter_by_session(self, kiso_client):
        await kiso_client.post(
            "/cron",
            json={"session": "filter-sess", "schedule": "*/5 * * * *",
                  "prompt": "Health check"},
            headers=AUTH_HEADER,
        )
        # Filter
        resp = await kiso_client.get(
            "/cron", params={"session": "filter-sess"}, headers=AUTH_HEADER,
        )
        jobs = resp.json()["jobs"]
        assert all(j["session"] == "filter-sess" for j in jobs)

    async def test_enable_disable(self, kiso_client):
        resp = await kiso_client.post(
            "/cron",
            json={"session": "toggle-sess", "schedule": "0 0 * * *",
                  "prompt": "Nightly"},
            headers=AUTH_HEADER,
        )
        job_id = resp.json()["id"]

        # Disable
        resp = await kiso_client.patch(
            f"/cron/{job_id}", params={"enabled": "false"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200

        # Re-enable
        resp = await kiso_client.patch(
            f"/cron/{job_id}", params={"enabled": "true"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200

    async def test_delete(self, kiso_client):
        resp = await kiso_client.post(
            "/cron",
            json={"session": "del-sess", "schedule": "0 12 * * *",
                  "prompt": "Noon check"},
            headers=AUTH_HEADER,
        )
        job_id = resp.json()["id"]

        resp = await kiso_client.delete(
            f"/cron/{job_id}", headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify gone
        resp = await kiso_client.get("/cron", headers=AUTH_HEADER)
        ids = [j["id"] for j in resp.json()["jobs"]]
        assert job_id not in ids

    async def test_invalid_schedule(self, kiso_client):
        resp = await kiso_client.post(
            "/cron",
            json={"session": "bad-sess", "schedule": "not valid",
                  "prompt": "Bad"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 400


# ── Safety Rules API ──────────────────────────────────────────


class TestSafetyRulesAPI:
    async def test_create_and_list(self, kiso_client):
        resp = await kiso_client.post(
            "/safety-rules",
            json={"content": "Never delete /etc files"},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["content"] == "Never delete /etc files"

        # List
        resp = await kiso_client.get("/safety-rules", headers=AUTH_HEADER)
        rules = resp.json()["rules"]
        assert any("delete /etc" in r["content"] for r in rules)

    async def test_delete_rule(self, kiso_client):
        resp = await kiso_client.post(
            "/safety-rules",
            json={"content": "Temporary rule for test"},
            headers=AUTH_HEADER,
        )
        rule_id = resp.json()["id"]

        resp = await kiso_client.delete(
            f"/safety-rules/{rule_id}", headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify gone
        resp = await kiso_client.get("/safety-rules", headers=AUTH_HEADER)
        ids = [r["id"] for r in resp.json()["rules"]]
        assert rule_id not in ids

    async def test_empty_content_rejected(self, kiso_client):
        resp = await kiso_client.post(
            "/safety-rules",
            json={"content": "  "},
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 400

    async def test_delete_nonexistent(self, kiso_client):
        resp = await kiso_client.delete(
            "/safety-rules/99999", headers=AUTH_HEADER,
        )
        assert resp.status_code == 404
