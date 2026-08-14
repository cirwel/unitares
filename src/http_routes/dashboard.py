"""Dashboard static/redesign serving, /phase view, retired-classic redirect.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.responses import JSONResponse, RedirectResponse, Response


from src.logging_utils import get_logger


logger = get_logger(__name__)


# Dashboard endpoint
async def http_phase(request):
    """Serve the phase-space visualization"""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    phase_path = Path(__file__).resolve().parents[2] / "dashboard" / "phase.html"
    if phase_path.exists():
        html = phase_path.read_text()
        if http_api_token:
            token_script = (
                f'<script>if(!localStorage.getItem("unitares_api_token"))'
                f'{{localStorage.setItem("unitares_api_token","{http_api_token}")}}</script>'
            )
            html = html.replace("</head>", f"{token_script}</head>", 1)
        return Response(content=html, media_type="text/html")
    return JSONResponse({"error": "Phase view not found", "path": str(phase_path)}, status_code=404)


# Dashboard static files. The classic dashboard was retired (see
# dashboard/README.md); the only top-level asset still served from
# /dashboard/<file> is phase.js, loaded by the standalone /phase view. The
# redesign serves its own assets via http_dashboard_redesign.
async def http_dashboard_static(request):
    """Serve dashboard static files"""
    file_path = request.path_params.get("file", "")
    if not file_path or ".." in file_path:
        return JSONResponse({"error": "Invalid file path"}, status_code=400)

    # Only allow specific files for security
    allowed_files = [
        "phase.js",
    ]
    if file_path not in allowed_files:
        return JSONResponse({
            "error": "File not allowed",
            "requested": file_path,
            "allowed": allowed_files
        }, status_code=403)

    static_path = Path(__file__).resolve().parents[2] / "dashboard" / file_path
    if static_path.exists() and static_path.is_file():
        # Determine content type
        content_type = "application/javascript"
        if file_path.endswith(".css"):
            content_type = "text/css"
        elif file_path.endswith(".json"):
            content_type = "application/json"

        return Response(
            content=static_path.read_text(),
            media_type=content_type,
            headers={"Cache-Control": "no-cache"},
        )
    return JSONResponse({
        "error": "File not found",
        "path": str(static_path)
    }, status_code=404)


# Redesign reference — additive preview path. Serves dashboard/redesign/** on
# the live origin so the redesign renders on real governance data. This is a
# NON-destructive preview: the production /dashboard route and index.html are
# untouched, and the route is fully reversible (remove it and nothing changes).
async def http_dashboard_redesign(request):
    """Serve the buildless dashboard redesign reference at /dashboard/redesign/."""
    rel = request.path_params.get("file", "") or "app.html"
    if ".." in rel or rel.startswith("/"):
        return JSONResponse({"error": "Invalid file path"}, status_code=400)
    # Auth HTML is served only through the gated /auth handlers. Shared CSS/JS
    # remains available here so those standalone pages stay buildless.
    if rel in {"auth/signin.html", "auth/enroll.html"}:
        return JSONResponse({"error": "File not found", "requested": rel}, status_code=404)
    if not rel.endswith((".html", ".css", ".js", ".md")):
        return JSONResponse({"error": "File type not allowed", "requested": rel}, status_code=403)

    base = (Path(__file__).resolve().parents[2] / "dashboard" / "redesign").resolve()
    target = (base / rel).resolve()
    if not str(target).startswith(str(base) + os.sep) or not target.is_file():
        return JSONResponse({"error": "File not found", "requested": rel}, status_code=404)

    media = {
        ".html": "text/html", ".css": "text/css",
        ".js": "application/javascript", ".md": "text/markdown",
    }[target.suffix]
    content = target.read_text()
    if target.suffix == ".html":
        # Cache-bust the relative asset refs with ?v=<max mtime of the redesign
        # tree>. no-cache without a validator lets browsers serve stale JS/CSS
        # (e.g. a months-old agents.js that crashes the table); a version query
        # forces a fresh fetch after every deploy. Absolute CDN refs are untouched.
        import re as _re
        try:
            _v = str(int(max((f.stat().st_mtime for f in base.rglob("*") if f.is_file()), default=0)))
            content = _re.sub(r'(src|href)="(\./[^"?]+)"', rf'\1="\2?v={_v}"', content)
        except Exception:
            pass
        # The entry page uses relative asset paths (./tokens.css, ./sections/*.js)
        # so it stays portable when opened as a file. Served at /dashboard/redesign
        # (no trailing slash), the browser would resolve those against /dashboard/.
        # Pin the base so relative paths resolve to the redesign subtree regardless
        # of trailing slash; absolute API calls (/v1/…, /api/…) are unaffected.
        content = content.replace(
            "<head>", '<head>\n<base href="/dashboard/redesign/">', 1
        )
        # Inject the API token so data.js authenticates live calls.
        http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
        if http_api_token:
            token_script = (
                f'<script>localStorage.setItem("unitares_api_token","{http_api_token}")</script>'
            )
            content = content.replace("</head>", f"{token_script}</head>", 1)
    # The entry HTML must NEVER be cached: it carries the ?v=<mtime> asset refs,
    # so a stale app.html would point at stale JS/CSS even though those are
    # version-busted. no-store guarantees a fresh entry on every load; the
    # versioned assets keep no-cache (their URL changes per deploy).
    cache = "no-store" if target.suffix == ".html" else "no-cache"
    return Response(content=content, media_type=media, headers={"Cache-Control": cache})


async def http_dashboard_classic_redirect(request):
    """Classic was retired (PR #1012). Old /dashboard/classic links land on the
    live dashboard instead of a confusing 403 from the static allowlist."""
    return RedirectResponse("/dashboard", status_code=302)
