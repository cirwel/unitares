"""Deployment docs must agree with the templates they describe.

The doc lint (`check_doc_health.py`) only denies phrasings already known to be
wrong, so a doc that misdescribes a shipped template passes until someone adds
that exact phrase. These checks read the templates themselves and hold three
structured claims to them: a "template sets `KEY=value`" sentence must match
`scripts/ops/com.unitares.governance-mcp.plist` (plistlib); the cloud runbook's
"replace every development default" list must name every published Compose
secret in `src/insecure_defaults.py::PUBLISHED_DEFAULT_SHA256` and must not
present the server-generated continuity secret as a shipped default; and an
"every service ... `cap_drop`" claim must name every service and every
capability that `docker-compose.yml` adds back with `cap_add` (yaml). Sources
are parsed, never imported, so the test needs no server, network or database.
"""

from __future__ import annotations

import ast
import importlib.util
import plistlib
import re
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLIST_TEMPLATE = REPO_ROOT / "scripts" / "ops" / "com.unitares.governance-mcp.plist"
COMPOSE = REPO_ROOT / "docker-compose.yml"
INSECURE_DEFAULTS = REPO_ROOT / "src" / "insecure_defaults.py"
CONTINUITY_SECRET = REPO_ROOT / "src" / "continuity_secret.py"
_DOC_HEALTH = REPO_ROOT / "scripts" / "diagnostics" / "check_doc_health.py"

PLIST_REL = "scripts/ops/com.unitares.governance-mcp.plist"
COMPOSE_REL = "docker-compose.yml"
INSECURE_REL = "src/insecure_defaults.py::PUBLISHED_DEFAULT_SHA256"
CONTINUITY_REL = "src/continuity_secret.py::ENV"

# Docs whose bare word "plist" means the shipped governance-mcp template. In
# other docs "the plist" is usually the maintainer's live LaunchAgent, which
# may legitimately differ, so only "template" is read there.
DEPLOY_DOCS = (
    "docs/manual/02-install.md",
    "docs/manual/03-running-the-server.md",
    "docs/install/PLAYBOOK.md",
    "docs/operations/cloud-deploy-runbook.md",
    "docs/DEPLOYMENT_SECURITY.md",
)

_SECRET_SHAPED = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|_URL$")


# --------------------------------------------------------------------------
# Sources of truth (parsed, never imported)
# --------------------------------------------------------------------------


@lru_cache(maxsize=None)
def template_env() -> dict[str, str]:
    with PLIST_TEMPLATE.open("rb") as fh:
        return dict(plistlib.load(fh)["EnvironmentVariables"])


@lru_cache(maxsize=None)
def compose_services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _module_assign(path: Path, name: str):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.relative_to(REPO_ROOT)} no longer assigns {name}")


@lru_cache(maxsize=None)
def published_default_names() -> frozenset[str]:
    return frozenset(_module_assign(INSECURE_DEFAULTS, "PUBLISHED_DEFAULT_SHA256"))


@lru_cache(maxsize=None)
def generated_secret_names() -> frozenset[str]:
    """Secrets the server generates itself when the variable is left empty."""
    return frozenset({_module_assign(CONTINUITY_SECRET, "ENV")})


def _shown(key: str, value: str) -> str:
    return "<redacted>" if _SECRET_SHAPED.search(key) else repr(value)


# --------------------------------------------------------------------------
# Markdown helpers
# --------------------------------------------------------------------------

_CODE_SPAN = re.compile(r"`[^`\n]*`")
_SENTENCE_END = re.compile(r"[.!?](?=\s+[A-Z`*(\[])")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")


def _mask(text: str) -> str:
    """Blank out code-span contents so dots inside `127.0.0.1` never split."""
    return _CODE_SPAN.sub(lambda m: "`" + "x" * (len(m.group()) - 2) + "`", text)


def _blocks(text: str) -> list[tuple[int, str]]:
    """Paragraphs and list items outside fenced code, with their first line."""
    blocks: list[tuple[int, str]] = []
    cur: list[str] = []
    start = 0
    in_fence = False

    def flush():
        if cur:
            blocks.append((start, "\n".join(cur)))
            cur.clear()

    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip() or _LIST_ITEM.match(line) or line.lstrip().startswith("#"):
            flush()
            if not line.strip():
                continue
        if not cur:
            start = i
        cur.append(line)
    flush()
    return blocks


def _block_sentences(start: int, block: str):
    """Yield (line, sentence, masked sentence) for each sentence of one block."""
    masked = _mask(block)
    pos = 0
    for end in [m.end() for m in _SENTENCE_END.finditer(masked)] + [len(masked)]:
        if end > pos:
            lead = len(block[pos:end]) - len(block[pos:end].lstrip())
            yield start + block.count("\n", 0, pos + lead), block[pos:end], masked[pos:end]
        pos = end


def _sentences(text: str):
    for start, block in _blocks(text):
        yield from _block_sentences(start, block)


# --------------------------------------------------------------------------
# (a) "template sets `KEY=value`" must match the plist template
# --------------------------------------------------------------------------

_KV = re.compile(r"`([A-Z][A-Z0-9_]*)=([^`]*)`")
_CLAUSE_END = re.compile(r";|—| -- |,\s*so\b|\bor\b|\bbut\b|\bunless\b")


def check_template_value_claims(rel: str, text: str) -> list[str]:
    subject = r"template" if rel not in DEPLOY_DOCS else r"template|plist"
    claim = re.compile(
        rf"\b(?:{subject})\b[^.`]{{0,40}}?\b(?:sets|has|ships|keeps|carries|contains|defaults)\b",
        re.IGNORECASE,
    )
    env = template_env()
    failures = []
    for line, sentence, masked in _sentences(text):
        for m in claim.finditer(masked):
            stop = _CLAUSE_END.search(masked, m.end())
            clause = sentence[m.end(): stop.start() if stop else len(sentence)]
            for key, value in _KV.findall(clause):
                value = value.strip().strip("'\"")
                if key not in env:
                    failures.append(
                        f"{rel}:{line}: claims the template sets `{key}={value}`, "
                        f"but {PLIST_REL} EnvironmentVariables has no {key}"
                    )
                elif env[key] != value:
                    failures.append(
                        f"{rel}:{line}: claims the template sets `{key}={_shown(key, value)}`, "
                        f"but {PLIST_REL} EnvironmentVariables sets {key}={_shown(key, env[key])}"
                    )
    return failures


# --------------------------------------------------------------------------
# (c) the runbook's "replace every development default" list
# --------------------------------------------------------------------------

_DEV_DEFAULT_INTRO = re.compile(r"replace every development default", re.IGNORECASE)
_BULLET = re.compile(r"^(\s*)- (.*)$")
_LEADING_NAMES = re.compile(r"^((?:`[A-Z_][A-Z0-9_]*`\s*(?:/|,|and)?\s*)+)")
_NOT_SHIPPED = re.compile(r"not a (?:shipped|development|compose) default", re.IGNORECASE)


def _dev_default_bullets(text: str) -> list[tuple[int, list[str], str]]:
    """(line, variable names, bullet text) for each bullet under the intro."""
    lines = text.splitlines()
    out: list[tuple[int, list[str], str]] = []
    for idx, line in enumerate(lines):
        if not _DEV_DEFAULT_INTRO.search(line):
            continue
        j = idx + 1
        while j < len(lines) and not _BULLET.match(lines[j]):
            if not lines[j].strip():
                break
            j += 1
        indent = None
        while j < len(lines):
            m = _BULLET.match(lines[j])
            if not m or (indent is not None and len(m.group(1)) != indent):
                break
            indent = len(m.group(1))
            body = [m.group(2)]
            k = j + 1
            while k < len(lines) and lines[k].strip() and not _BULLET.match(lines[k]) \
                    and len(lines[k]) - len(lines[k].lstrip()) > indent:
                body.append(lines[k].strip())
                k += 1
            bullet = " ".join(body)
            names: list[str] = []
            lead = _LEADING_NAMES.match(bullet)
            for raw in re.findall(r"`([A-Z_][A-Z0-9_]*)`", lead.group(1) if lead else ""):
                if raw.startswith("_") and names:
                    raw = names[-1].rsplit("_", 1)[0] + raw
                names.append(raw)
            out.append((j + 1, names, bullet))
            j = k
    return out


def check_dev_default_list(rel: str, text: str) -> list[str]:
    bullets = _dev_default_bullets(text)
    if not bullets:
        return []
    failures = []
    listed = {name for _, names, _ in bullets for name in names}
    intro_line = bullets[0][0] - 1
    for name in sorted(published_default_names() - listed):
        failures.append(
            f"{rel}:{intro_line}: the 'replace every development default' list omits "
            f"`{name}`, a published Compose default per {INSECURE_REL}"
        )
    for line, names, bullet in bullets:
        for name in names:
            if name in generated_secret_names() and not _NOT_SHIPPED.search(bullet):
                failures.append(
                    f"{rel}:{line}: lists `{name}` as a development default to replace, "
                    f"but {COMPOSE_REL} ships it empty and the server generates it "
                    f"({CONTINUITY_REL}); say it is not a shipped default"
                )
    return failures


# --------------------------------------------------------------------------
# (d) "every service ... cap_drop" must account for every cap_add
# --------------------------------------------------------------------------

_EVERY_SERVICE = re.compile(
    r"\b(?:every|each|all(?: (?:four|the))?)\s+(?:compose\s+)?services?\b(\s*\([^)]*\))?",
    re.IGNORECASE,
)


def check_cap_drop_claims(rel: str, text: str) -> list[str]:
    services = compose_services()
    failures = []
    for start, block in _blocks(text):
        for line, sentence, masked in _block_sentences(start, block):
            claim = _EVERY_SERVICE.search(masked)
            if not (claim and "`cap_drop" in sentence):
                continue
            listed = sentence[claim.start(1): claim.end(1)] if claim.group(1) else ""
            named = set(re.findall(r"`([a-z][a-z0-9-]*)`", listed))
            if named and named != set(services):
                failures.append(
                    f"{rel}:{line}: 'every service' names {sorted(named)}, "
                    f"but {COMPOSE_REL} defines {sorted(services)}"
                )
            # The rest of the block, without the service list, must account
            # for every capability a service adds back.
            rest = block.replace(listed, "", 1) if listed else block
            for name, svc in sorted(services.items()):
                if "ALL" not in (svc.get("cap_drop") or []):
                    failures.append(
                        f"{rel}:{line}: claims every service drops ALL capabilities, "
                        f"but {COMPOSE_REL} service {name} has cap_drop={svc.get('cap_drop')}"
                    )
                added = svc.get("cap_add") or []
                unnamed = [c for c in added if not re.search(rf"\b{c}\b", rest)]
                if added and not re.search(rf"\b{re.escape(name)}\b", rest):
                    unnamed.insert(0, f"service {name}")
                if unnamed:
                    failures.append(
                        f"{rel}:{line}: claims every service runs with `cap_drop: [ALL]` "
                        f"but does not say that {COMPOSE_REL} service {name} adds back "
                        f"cap_add={added} (not named: {unnamed})"
                    )
    return failures


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def living_docs() -> list[tuple[str, str]]:
    spec = importlib.util.spec_from_file_location("check_doc_health_deploy", _DOC_HEALTH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    docs = []
    for path in module.collect_md_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        docs.append((rel, path.read_text(errors="replace")))
    rels = {rel for rel, _ in docs}
    missing = [d for d in DEPLOY_DOCS if d not in rels]
    assert not missing, f"deployment docs left the living-doc set: {missing}"
    return docs


def test_template_value_claims_match_the_plist(living_docs):
    failures = []
    for rel, text in living_docs:
        if rel in DEPLOY_DOCS or "com.unitares.governance-mcp.plist" in text:
            failures += check_template_value_claims(rel, text)
    assert not failures, "\n".join(failures)


def test_dev_default_list_matches_published_compose_defaults(living_docs):
    failures = []
    found = False
    for rel, text in living_docs:
        found |= bool(_dev_default_bullets(text))
        failures += check_dev_default_list(rel, text)
    assert found, "no doc has a 'replace every development default' list any more"
    assert not failures, "\n".join(failures)


def test_cap_drop_claims_account_for_cap_add(living_docs):
    failures = []
    for rel, text in living_docs:
        failures += check_cap_drop_claims(rel, text)
    assert not failures, "\n".join(failures)


def test_generated_secret_is_not_a_compose_default():
    # The runbook rule above rests on this: Compose ships the generated
    # secret empty, so listing it as a shipped default is wrong.
    for name in generated_secret_names():
        for svc_name, svc in compose_services().items():
            value = (svc.get("environment") or {}).get(name)
            if value is None:
                continue
            assert not re.search(rf"\$\{{{name}:-[^}}]+\}}", str(value)), (
                f"{COMPOSE_REL} service {svc_name} now ships a default for {name}; "
                f"{CONTINUITY_REL} treats that variable as server-generated"
            )


# Regression cases: the stale text each check was written to catch.

def test_flags_stale_bind_all_interfaces_claim():
    stale = (
        "The template sets `UNITARES_BIND_ALL_INTERFACES=1` and passes no `--host`, "
        "so the rendered plist binds `0.0.0.0`, not loopback — remove that key, or "
        "set `UNITARES_MCP_HOST=127.0.0.1`, for a loopback-only install.\n"
    )
    failures = check_template_value_claims("docs/manual/03-running-the-server.md", stale)
    assert len(failures) == 1
    assert "docs/manual/03-running-the-server.md:1" in failures[0]
    assert "UNITARES_BIND_ALL_INTERFACES='0'" in failures[0]
    fixed = stale.replace("_INTERFACES=1`", "_INTERFACES=0`")
    assert check_template_value_claims("docs/manual/03-running-the-server.md", fixed) == []


def test_template_instruction_is_not_a_claim():
    text = "To expose the server, set `UNITARES_BIND_ALL_INTERFACES=1` in the rendered plist.\n"
    assert check_template_value_claims("docs/manual/03-running-the-server.md", text) == []


def test_flags_continuity_secret_listed_as_dev_default():
    stale = (
        "3. `cp .env.example .env` and **replace every development default**. The\n"
        "   Compose file ships loopback-only dev values:\n"
        "   - `POSTGRES_PASSWORD` — non-default.\n"
        "   - `LEASE_PLANE_BEARER_TOKEN` — unique random value.\n"
        "   - `UNITARES_CONTINUITY_TOKEN_SECRET` — unique random secret.\n"
        "   - `UNITARES_LEASE_ATTESTATION_ISSUER` / `_AUDIENCE` — stable ids.\n"
        "4. `docker compose up -d --build`.\n"
    )
    failures = check_dev_default_list("docs/operations/cloud-deploy-runbook.md", stale)
    joined = "\n".join(failures)
    assert "cloud-deploy-runbook.md:5: lists `UNITARES_CONTINUITY_TOKEN_SECRET`" in joined
    assert "omits `UNITARES_LEASE_ATTESTATION_SIGNING_KEY`" in joined
    names = {n for _, ns, _ in _dev_default_bullets(stale) for n in ns}
    assert "UNITARES_LEASE_ATTESTATION_AUDIENCE" in names


def test_flags_every_service_cap_drop_without_redis_cap_add():
    stale = (
        "- Every service (`postgres-age`, `redis`, `lease-plane`, `governance-mcp`)\n"
        "  runs with `security_opt: [no-new-privileges:true]` and `cap_drop: [ALL]`.\n"
        "  Verified against `docker compose up`: all four still start.\n"
    )
    failures = check_cap_drop_claims("docs/DEPLOYMENT_SECURITY.md", stale)
    assert len(failures) == 1
    assert "docs/DEPLOYMENT_SECURITY.md:1" in failures[0] and "service redis" in failures[0]
    fixed = stale.replace(
        "  Verified",
        "  `redis` then adds back `SETUID`, `SETGID`, `CHOWN` and `FOWNER`.\n  Verified",
    )
    assert check_cap_drop_claims("docs/DEPLOYMENT_SECURITY.md", fixed) == []
