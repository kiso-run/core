"""Tests for M18 — published files (store + endpoint)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from kiso.store import get_published_file, init_db, publish_file


# --- store functions ---


class TestPublishFile:
    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        yield conn
        await conn.close()

    async def test_returns_uuid(self, db):
        file_id = await publish_file(db, "sess1", "report.pdf", "/tmp/report.pdf")
        # Should be a valid UUID4
        parsed = uuid.UUID(file_id, version=4)
        assert str(parsed) == file_id

    async def test_retrievable(self, db):
        file_id = await publish_file(db, "sess1", "image.png", "/data/image.png")
        row = await get_published_file(db, file_id)
        assert row is not None
        assert row["session"] == "sess1"
        assert row["filename"] == "image.png"
        assert row["path"] == "/data/image.png"

    async def test_unique_ids(self, db):
        id1 = await publish_file(db, "sess1", "a.txt", "/a.txt")
        id2 = await publish_file(db, "sess1", "b.txt", "/b.txt")
        assert id1 != id2

    async def test_not_found(self, db):
        row = await get_published_file(db, str(uuid.uuid4()))
        assert row is None

    async def test_has_created_at(self, db):
        file_id = await publish_file(db, "sess1", "f.txt", "/f.txt")
        row = await get_published_file(db, file_id)
        assert row["created_at"] is not None


# --- GET /pub/{id} endpoint ---


class TestGetPubEndpoint:
    async def test_serves_file(self, client, tmp_path):
        db = client._transport.app.state.db  # type: ignore[attr-defined]
        # Create a real file
        pub_dir = tmp_path / "pub"
        pub_dir.mkdir()
        test_file = pub_dir / "hello.txt"
        test_file.write_text("Hello, world!")

        file_id = await publish_file(db, "sess1", "hello.txt", str(test_file))

        resp = await client.get(f"/pub/{file_id}")
        assert resp.status_code == 200
        assert resp.text == "Hello, world!"
        assert "hello.txt" in resp.headers.get("content-disposition", "")

    async def test_content_type_from_filename(self, client, tmp_path):
        db = client._transport.app.state.db  # type: ignore[attr-defined]
        pub_dir = tmp_path / "pub"
        pub_dir.mkdir()
        test_file = pub_dir / "data.json"
        test_file.write_text('{"key": "value"}')

        file_id = await publish_file(db, "sess1", "data.json", str(test_file))

        resp = await client.get(f"/pub/{file_id}")
        assert resp.status_code == 200
        assert "json" in resp.headers.get("content-type", "")

    async def test_unknown_extension_octet_stream(self, client, tmp_path):
        db = client._transport.app.state.db  # type: ignore[attr-defined]
        pub_dir = tmp_path / "pub"
        pub_dir.mkdir()
        test_file = pub_dir / "data.xyz123"
        test_file.write_bytes(b"\x00\x01\x02")

        file_id = await publish_file(db, "sess1", "data.xyz123", str(test_file))

        resp = await client.get(f"/pub/{file_id}")
        assert resp.status_code == 200
        assert "octet-stream" in resp.headers.get("content-type", "")

    async def test_not_found_uuid(self, client):
        resp = await client.get(f"/pub/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_file_deleted_from_disk(self, client, tmp_path):
        """DB entry exists but file was removed from disk → 404."""
        db = client._transport.app.state.db  # type: ignore[attr-defined]
        pub_dir = tmp_path / "pub"
        pub_dir.mkdir()
        test_file = pub_dir / "gone.txt"
        test_file.write_text("bye")

        file_id = await publish_file(db, "sess1", "gone.txt", str(test_file))
        test_file.unlink()  # remove the file

        resp = await client.get(f"/pub/{file_id}")
        assert resp.status_code == 404

    async def test_no_auth_required(self, client, tmp_path):
        """The /pub endpoint works without an Authorization header."""
        db = client._transport.app.state.db  # type: ignore[attr-defined]
        pub_dir = tmp_path / "pub"
        pub_dir.mkdir()
        test_file = pub_dir / "open.txt"
        test_file.write_text("public")

        file_id = await publish_file(db, "sess1", "open.txt", str(test_file))

        # Explicitly no auth header
        resp = await client.get(f"/pub/{file_id}", headers={})
        assert resp.status_code == 200
