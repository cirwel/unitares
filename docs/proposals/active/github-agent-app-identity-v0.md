# Separate GitHub automation identity from the operator

**Status:** Attribution-first direction approved by Kenny on 2026-10-13
(“proceed” after the KG-audit recommendations). Registration and local credential
wiring remain pending. Actor-based authorization is expressly deferred.

## Problem

Agents and Kenny currently write through the same GitHub account. The PR-body
ownership footer identifies the UNITARES process but cannot prove that a
GitHub action was performed by the human. The KG audit addendum explicitly asks
for one App identity while the operator remains `cirwel`.

The footer decision is KG `2026-10-13T09:24:42.365405+00:00`: session link when
available, then `UNITARES agent: <uuid>`. It is attribution, not authorization;
links stay out of commits and committed files. No actual session link is stored
in this proposal.

## Proposed boundary

Use one repository-scoped GitHub App installation for supported local agent
clients and automation. Its installation token identifies a bot; a user access
token acts on behalf of a user and would not provide the intended separation.
Limit installation access to selected repositories and required permissions.
These mechanisms are documented by GitHub in
[Apps versus OAuth](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps)
and [installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).

Keep the private key in operator-controlled credential custody; issue short-lived
installation tokens through a local broker or trusted runner. Agents should not
receive a reusable App private key or install/configure Apps. The App receives
ordinary PR/issue/content permissions only after enumerating the current CLI
calls; no administration or branch-protection bypass by default. Existing CI,
review and queue rules still apply. Token scope, expiry, rotation, revocation,
fork behavior and audit attribution need a tested rollout plan.

GitHub actor separates bot from human **only if the human credential is exclusive
to the human**. Per-session identity stays in UNITARES via the PR footer; one App
does not create a cryptographically distinct GitHub principal for each session.
The footer must never grant operator privileges or branch ownership on its own.

## Cloud Claude limitation and operator-only acts

The operator's current cloud Claude sessions post through `cirwel` credentials
and cannot use this App (a deployment constraint supplied in the handoff, not a
claim about every future cloud product). Therefore `actor == cirwel` cannot
enforce operator-only acts while that arrangement remains. Renaming local bots
does not solve this exception, and a bot/user string allowlist would be unsafe.

Options for the operator:

1. Adopt the App for local attribution first, explicitly retaining advisory
   operator-only checks until cloud access can be isolated.
2. Isolate/revoke cloud access to operator-sensitive actions before enforcing
   actor-based checks. Verify actual credentials and permission scopes, not
   commit authors or PR-body claims.
3. Require a separate operator-held approval credential/channel for reserved
   acts, bound to repository, PR head SHA and the exact act. Cloud sessions can
   still prepare ordinary PRs, but cannot mint that approval. This is a second
   identity/authorization design needing its own reviewed protocol.

Recommendation: stage attribution first; do not claim actor-only enforcement
until option 2 or 3 resolves the cloud exception. No credential inspection,
App registration, token minting, ruleset update or live enforcement test was done.

## Acceptance and decision

Test permitted and forbidden actions for human, App, cloud-as-human and fork
actors; prove cloud cannot self-approve a reserved action; verify exact-head
binding, token expiry/revocation, queue behavior and break-glass recovery. Map
any new public capability against [INTENT_MAP](../../integration/INTENT_MAP.md)
before implementation. Preserve UNITARES ownership and lineage rules.

## Approved first rollout

Start with one private App, `UNITARES agents`, installed only on the selected
`cirwel/unitares` repository. Add other repositories deliberately after its
smoke test. No webhooks, OAuth-on-install, account permissions, administration,
or ruleset bypass. Proposed repository permissions are:

| Permission | Level | Required operation |
|---|---|---|
| Contents | write | Fetch and push agent branches |
| Pull requests | write | Open/edit draft PRs and write reviews |
| Issues | write | PR labels and issue comments |
| Checks | read | Inspect CI results |
| Commit statuses | read | Inspect legacy status contexts |
| Actions | read | Read workflow runs and logs |
| Metadata | read | Repository identification (implicit) |

Workflow-file pushes are intentionally outside this first permission set. If
needed, request the separate Workflows write grant; do not fall back silently
to the human credential. Neither the permission grant nor a bot actor permits
bypassing branch ownership, reviews or the merge queue.

[Open the prefilled registration](https://github.com/settings/apps/new?name=UNITARES+agents&url=https%3A%2F%2Fgithub.com%2Fcirwel%2Funitares&description=Attribution+for+UNITARES+agent+development%3B+no+operator+authority.&public=false&webhook_active=false&request_oauth_on_install=false&contents=write&pull_requests=write&issues=write&checks=read&statuses=read&actions=read).
Review the settings in GitHub, create the App, and install it on the selected
repository. The name may need a unique suffix. GitHub's
[documented URL parameters](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-using-url-parameters)
preselect settings; they do not create the App or prove installation.

The current CLI token returned HTTP 403 for `GET /user/installations`: it is
not authorized to a GitHub App. This does not prove no App exists. No private
key or token was read, generated, committed or installed in this follow-up.
The operator's registration screen is the remaining external setup boundary;
no desktop/tab access was used.

After registration, keep the private key in operator-controlled custody. A
trusted broker/runner may mint repository-scoped installation tokens and give
agents only those expiring tokens. Configure `GH_TOKEN` only for the agent
process, leaving the operator's global `gh` login intact. Never write a token
into a remote URL, a command log, shell history, the graph or a PR body.
Do not implement a reusable-key reader in an agent worktree as a broker.

Before general rollout, prove a bot-attributed draft PR on an agent-owned smoke
branch, a permitted push and label operation, CI visibility, token expiry, and
denial on an unselected repository. Verify the human's normal CLI remains
`cirwel`. Record evidence on the original KG audit, then migrate supported
clients individually. Until those checks pass, this is prepared configuration,
not a live bot identity. Cloud Claude's limitation and all operator-only acts
retain the existing policy; actor `cirwel` alone remains insufficient proof.
