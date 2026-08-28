"""Dashboard static/redesign serving, /phase view, retired-classic redirect.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.responses import JSONResponse, RedirectResponse, Response


from src.logging_utils import get_logger
from src.http_routes import access


logger = get_logger(__name__)

# Files under dashboard/redesign/ that carry governance data rather than
# presentation, and are therefore served only to an authenticated caller.
# The deeper fix is for the fallback bundle to hold synthetic data instead of
# a real capture; until then it is gated like the endpoints it mirrors.
_AUTHENTICATED_ONLY_FILES = {"snapshot.js"}


# Dashboard endpoint
async def http_phase(request):
    """Serve the phase-space visualization"""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    # The shell itself stays public (the sign-in page must be reachable), but
    # the convenience token is injected ONLY for a caller that is already
    # trusted or signed in. Injecting it unconditionally published the local
    # bearer to any anonymous caller — over a tunnel, to the internet — which
    # handed out the exact credential every gated route checks.
    may_receive_token = not access.mcp_bearer_required() and access._check_http_auth(
        request, http_api_token=http_api_token
    )
    phase_path = Path(__file__).resolve().parents[2] / "dashboard" / "phase.html"
    if phase_path.exists():
        html = phase_path.read_text()
        if http_api_token and may_receive_token:
            token_script = (
                f'<script>if(!localStorage.getItem("unitares_api_token"))'
                f'{{localStorage.setItem("unitares_api_token","{http_api_token}")}}</script>'
            )
            html = html.replace("</head>", f"{token_script}</head>", 1)
        # This response now VARIES by caller (the token is injected only for a
        # trusted or signed-in one), and it can carry a credential. A shared
        # cache in front of the server — this deployment sits behind a tunnel —
        # must neither store it nor serve one caller's copy to another.
        return Response(
            content=html,
            media_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Vary": "Cookie, Authorization",
            },
        )
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
    # Not every file under the shell is shell. snapshot.js is a bundled capture
    # of real governance state — resident ids and names, EISV vectors,
    # coherence, risk, verdicts, a multi-day trajectory — i.e. the same data
    # class the /v1/eisv/* routes are gated for, in a file the "static assets
    # are public" rule would wave straight through. app.html loads it with a
    # same-origin <script src>, which carries the session cookie, so a
    # signed-in operator and a loopback caller still get it; nobody else does.
    if rel in _AUTHENTICATED_ONLY_FILES:
        http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
        if not access._check_http_auth(request, http_api_token=http_api_token):
            return access._http_unauthorized()

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
        # Inject the API token so data.js authenticates live calls — but only
        # for a caller already trusted (loopback/tailnet) or signed in. See
        # http_phase above: an unconditional inject published the bearer to
        # anonymous callers. An unauthenticated browser still gets the shell;
        # its data calls 401 and authFetch redirects it to /auth/signin.
        # Not in hosted posture: there the gate accepts ONLY the MCP bearer, so
        # handing the browser UNITARES_HTTP_API_TOKEN gives it a credential
        # every gated route rejects — and a non-empty token also suppresses
        # data.js's 401 -> /auth/signin redirect, stranding the user on a
        # silently stale page instead of sending them to sign in.
        http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
        if (
            http_api_token
            and not access.mcp_bearer_required()
            and access._check_http_auth(request, http_api_token=http_api_token)
        ):
            token_script = (
                f'<script>localStorage.setItem("unitares_api_token","{http_api_token}")</script>'
            )
            content = content.replace("</head>", f"{token_script}</head>", 1)
    # The entry HTML must NEVER be cached: it carries the ?v=<mtime> asset refs,
    # so a stale app.html would point at stale JS/CSS even though those are
    # version-busted. no-store guarantees a fresh entry on every load; the
    # versioned assets keep no-cache (their URL changes per deploy).
    cache = "no-store" if target.suffix == ".html" else "no-cache"
    # Vary on the credential inputs: both the entry HTML (which may carry the
    # injected token) and snapshot.js (gated above) now differ per caller, so a
    # shared cache must key on them rather than serve one caller's copy to the
    # next. no-store on the HTML already prevents storage; Vary covers the rest.
    return Response(
        content=content,
        media_type=media,
        headers={"Cache-Control": cache, "Vary": "Cookie, Authorization"},
    )


async def http_dashboard_classic_redirect(request):
    """Classic was retired (PR #1012). Old /dashboard/classic links land on the
    live dashboard instead of a confusing 403 from the static allowlist."""
    return RedirectResponse("/dashboard", status_code=302)
