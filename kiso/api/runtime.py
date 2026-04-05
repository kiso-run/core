"""Runtime-facing API routes."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse

import kiso.main as main_mod

router = APIRouter()


@router.get("/health")
async def health():
    from kiso._version import __version__
    from kiso.sysenv import get_resource_limits

    rl = get_resource_limits()
    max_disk = getattr(main_mod.app.state, "config", None)
    if max_disk is not None:
        max_disk = max_disk.settings.get("max_disk_gb")
    if max_disk is None:
        max_disk = rl["disk_total_gb"]
    return {
        "status": "ok",
        "version": __version__,
        "build_hash": os.environ.get("KISO_BUILD_HASH", "dev"),
        "resources": {
            "memory_mb": {"used": rl["memory_used_mb"], "limit": rl["memory_mb"]},
            "cpu": {"limit": rl["cpu_limit"]},
            "disk_gb": {"used": rl["disk_used_gb"], "limit": max_disk},
            "pids": {"used": rl["pids_used"], "limit": rl["pids_limit"]},
        },
    }


@router.get("/pub/{token}/{filename:path}")
async def get_pub(token: str, filename: str, request: Request):
    """Serve a file from a session's pub/ directory. No authentication required."""
    client_ip = request.client.host if request.client else "unknown"
    await main_mod._check_rate_limit(f"pub:{client_ip}")

    config = request.app.state.config
    session = main_mod.resolve_pub_token(token, config)
    if session is None:
        raise HTTPException(status_code=404, detail="Not found")

    pub_dir = main_mod.KISO_DIR / "sessions" / session / "pub"
    file_path = (pub_dir / filename).resolve()
    if not file_path.is_relative_to(pub_dir.resolve()):
        raise HTTPException(status_code=404, detail="Not found")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    response = FileResponse(path=file_path, filename=Path(filename).name, media_type=media_type)
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
