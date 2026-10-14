# Separate GitHub automation identity from the operator

**Status:** Attribution-first direction approved by Kenny on 2026-10-13.
The private App is registered and installed on this repository; the local
command runner authenticates as `unitares-agents[bot]`. Kenny subsequently
authorized registration, credential setup and local-client rollout. Bot write
and CI evidence belongs in the rollout PR. Actor-based authorization is deferred.

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

Adopted direction: stage attribution first; do not claim actor-only enforcement
until option 2 or 3 resolves the cloud exception. The initial proposal did no
credential or enforcement work; the authorized installation is recorded below.
No ruleset or operator-only enforcement change is part of this rollout.

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

Registration completed on 2026-10-13 using an operator-authorized new Chrome
tab and operator passkey confirmation. App ID **5306351**, installation ID
**171128073**; only `cirwel/unitares` is selected. These are public identifiers,
not credentials. The signing key is held in owner-only local configuration;
its fingerprint was verified and the unused first key was revoked. No private
key or installation token is committed, placed in a PR body, or stored in KG.

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

## Local command path and verification

The operator-local `unitares-github` runner lives in private host tooling and
is installed as a stable executable outside agent worktrees. It requests an
explicit single-repository installation token for each command, supplies only
that expiring token to the child, and revokes it on completion. Its default
permissions match the table; `--read-only` downgrades every permission.

```sh
unitares-github --read-only -- gh pr list
unitares-github -- git push -u origin codex/example-1013
unitares-github -- git prepr
unitares-github -- gh pr create --draft --body-file /path/to/body.md
unitares-github --read-only -- gh pr checks 123
```

Local commits remain ordinary Git operations. Use HTTPS remotes; the runner
rejects SSH and redirects, replaces inherited credential helpers, and refuses
HTTP extraheaders that could substitute a human identity. It preserves Git hooks and uses an
empty temporary `gh` configuration. No global login or credential helper is
replaced. A missing permission fails the command: never silently retry with
the human credential. Tokens expire after one hour; commands are bounded to
55 minutes, with revocation attempted even on failure or handled interruption.
An abrupt process kill can leave a token valid until expiry.

Initial live checks verified the App JWT, exactly one accessible repository,
CI reads (HTTP 200), denial on an unselected private repository (404), and
token revocation (204 followed by 401). The installed runner's GraphQL viewer
is `unitares-agents[bot]`; the unwrapped operator CLI remains `cirwel`. The
rollout PR supplies bot-authored draft-PR, push, label and CI-check evidence.
Natural one-hour expiry was not waited out; GitHub's returned expiry and
explicit revocation were checked separately.

Local Codex and local Claude share the host contract directing UNITARES
GitHub operations through this runner. Existing sessions need that instruction
in their current context; there is no automatic shell-command rewriting.
Other repositories and cloud Claude retain their existing paths. This is an
attribution convention, not OS isolation: same-user processes can still read
same-user files, so the runner cannot establish exclusive human credentials.
It grants no new authority to the PR footer or bot actor.
