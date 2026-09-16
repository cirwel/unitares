# Public presentation critique: cirwel.org and the UNITARES front doors

**Created:** 2026-09-16
**Status:** Review record, point in time. Nothing here changes runtime behavior.
**Scope:** `cirwel/cirwel-site` at `6f8d13e` (the four pages after the 2026-09-15 edits, PRs #46 to #48), the UNITARES surfaces that state what the product is (`README.md`, `docs/PRODUCT_DEFINITION.md`, `docs/public-site/index.md`, `CITATION.cff`, the social card), and `cirwel/cirwel.github.io` at `9cff70b`.
**Method:** every page source read in full; the site built locally with Astro and screenshotted at 1440 px and 390 px in both impressions; page copy measured with a small script (word count, negation density, self-reference count, term counts); the site's own claims register replayed against the local `unitares` checkout. The live site was not fetched (network policy), so anything that differs between the repository head and what is deployed is outside this record.

The brief this answers: communicate UNITARES without being verbose, convoluted, or internal, and without dumbing it down, in a way that many writers (Codex sessions, Claude sessions, the operator) can converge on rather than each rewriting in their own voice.

## Summary

1. **There is no single sentence for what UNITARES is, and the surfaces disagree.** Ten phrasings are live across the site, the repository, and the share cards, spanning four different frames (continuity, governance, telemetry, accountability infrastructure). The repository converged on "federation kernel, accountability infrastructure" on 2026-09-10 and 2026-09-13; the site edited on 2026-09-15 uses the word "federation" zero times on its home, ecosystem, and build pages.
2. **Three statements on the site are wrong or stale today.** The build page documents `GOVERNANCE_TOOL_MODE`, which #2137 removed on 2026-09-08. The share card and the page title carry the previous hero headline. Eight of the fifteen entries in the site's claims register point at copy that no longer exists, so the home page currently has zero mechanically checked claims.
3. **The prose is honest and over-hedged.** The research page carries 4.2 negation markers per 100 words and the build page 3.0; most claims are followed by their own disclaimer in the "X, not Y" form. The build page refers to itself as a site or page eight times. The list "identity, evidence, memory, runtime state, and coordination" appears nine times with a different membership almost every time.
4. **The design register is strong and is spent on the wrong things.** The type, the two impressions, print and reduced-motion handling are excellent. But the home page is 5,500 px of text with no diagram, the best figures in the repository are unused, one fact (running since November 2025) is rendered three times on one page, and the only links into the product are 12 px uppercase mono labels.
5. **The fix is a written editorial contract, not another rewrite.** PR #47 removed 1,675 lines and PR #48 restored 1,541 of them within a day. That is what happens when every session re-derives "effective" from scratch. The same move that keeps `AGENTS.md` and `CLAUDE.md` in step (a shared block every writer honors) is what the site needs.

## 1. The sentence

### What is live now

| Surface | Sentence |
|---|---|
| cirwel.org hero (`index.astro`) | "Agent continuity." |
| cirwel.org `<title>` | "Infrastructure for long-lived AI agents" |
| cirwel.org share card (`og.svg`, `og.png`) | "Infrastructure for long-lived AI agents." |
| cirwel.org JSON-LD, `og:image:alt`, `package.json` | "Runtime governance for heterogeneous AI-agent fleets." |
| cirwel.github.io (index hub) | "Infrastructure for long-lived AI agents." |
| `README.md` | "Accountability infrastructure for long-running AI agents." and "Its federation kernel connects independent runtimes to one operator-controlled server" |
| `docs/PRODUCT_DEFINITION.md` | "a self-hosted federation kernel for agent identity, claims and evidence, review, outcomes, and reconstruction" |
| Social card (`scripts/dev/brand/render_social_preview.py`) | "A federation kernel for accountable AI agents." |
| GitHub Pages landing (`docs/public-site/index.md`) | "Runtime governance for long-lived AI agents" |
| Glossary intro (`scripts/dev/build_public_site.py`) and `docs/REVIEWER_GUIDE.md` | "runtime state telemetry for long-lived AI agents" |

A visitor who follows the site to the repository, or the repository to the site, reads a different product each time. The share card that gets pasted into Slack says a fourth thing. None of these is wrong; the problem is that the reader cannot tell which one is the definition.

### Recommendation

Choose one sentence, own it in `unitares`, and have every surface copy it verbatim. The site's `check-claims.py` already pins strings to sources; the sentence should be one more pinned string, with the README as its source.

Proposed, in the register the site already uses (headline, then the sentence, then the boundary):

> **Many agents, one record.**
>
> UNITARES is a self-hosted record that independent AI agents share: who did what, what evidence backed it, who reviewed it, and what happened next, kept across the restarts and handoffs that the agents themselves do not survive.
>
> Claude Code, Codex, and custom runtimes keep their own models and tools. UNITARES keeps the record. It runs beside evals, guardrails, and sandboxes and replaces none of them.

This is the federation idea stated without the word "kernel": many independent runtimes, one operator-controlled record. "Federation" can appear once on the ecosystem page in exactly that plain sense. "Kernel" is a term of art and, if kept, must be defined in the same sentence it appears in, as `PRODUCT_DEFINITION.md` does.

The choice of sentence is an editorial decision for the operator. What is not optional is that there be one, and that it be the same everywhere.

## 2. The federation framing gap

`docs/PRODUCT_DEFINITION.md` already contains the clearest plain-language statement of the product in either repository: five questions, one deployed mechanism each, one boundary each.

| Question | Deployed mechanism |
|---|---|
| Who said it? | Fresh process identity and explicit lineage. |
| What supports it? | Derived check-in state, durable attributed findings, corrections, and provenance. |
| Who challenged it? | Structured review records, disagreement, conditions, and resolution. |
| What happened? | Typed outcome events and prediction binding. |
| What can a successor recover? | Shared-memory search, knowledge reads, review records, history export. |

The site does not use it. Instead the home page offers three columns ("Identity & evidence", "Memory & review", "State & coordination") and a noun list that changes membership on every page:

- home hero: identity, evidence, memory, runtime state, and coordination
- home §01: identity, evidence, memory, state, and responsibility
- ecosystem hero: identity, evidence, memory, runtime state, review, policy, and coordination
- ecosystem §01: identity, provenance, longitudinal state, policy and recovery, audit, shared knowledge, review, and coordination
- ecosystem receipts: identity, check-ins, evidence, memory, policy

A reader cannot tell whether the list is the definition or decoration. The five questions are the definition. They are not dumbed down (each has a real mechanism and a real boundary behind it) and they are not internal (no term in them needs the glossary). Use them as the single spine of the home page and of the ecosystem page's first section, and retire the noun list.

## 3. Errata: what is wrong on the site today

Ordered by cost to a reader.

1. **`GOVERNANCE_TOOL_MODE` no longer exists.** Build page §03 says a default profile advertises a subset of tools and the variable narrows or widens it. Since #2137 (2026-09-08) one complete catalog is advertised on every transport and the legacy setting is accepted and ignored (`src/tool_modes.py`, `docs/EVIDENCE_AND_LIMITS.md`). The paragraph is also the most convoluted on the site ("The profile decides what discovery advertises, never what dispatches, but schema-driven clients only offer a model what discovery returned"). Replace it with one sentence: every tool is advertised on every transport; there is no mode to configure.
2. **The share card and the title carry the previous headline.** The hero became "Agent continuity." in #47; `og.svg`/`og.png` and `<title>` still say "Infrastructure for long-lived AI agents". PR #41 (2026-09-12) established the rule that the card carries the current hero headline; it broke again three days later. The durable fix is the one `docs/assets/README.md` already records for the UNITARES mark: do not bake a sentence into an image, or render the card from the same constant the hero reads.
3. **The claims register is half stale.** `src/data/claims.json` has fifteen entries. Replayed against the current pages, eight no longer match the copy they claim to guard: all four home entries (`leg-c-persistence`, `leg-c-verdict-scope`, `outcome-unresolved`, `december-read`), two research entries (`research-outcome-withdrawn`, `research-sensitivity-bound`), and both ecosystem entries (`workloads-not-the-contract`, `workload-entry-point`). The December 2026 pre-registered read moved from the home page to the research page, but its entry still points at home. The checker reports these as warnings and exits 0, so the daily run passes while the home page has no checked claim at all. Re-pin each entry to the page and sentence that now carries it, or delete it; add entries for the sentences that are now load-bearing (the pre-registered read on research, "since November 2025" on home).
4. **The errata record was removed while the principle stayed.** #47 deleted `Errata.astro` and `revisions.json`. The component's own header said a publication that revises silently is asking to be taken on faith, "the one thing this site argues against", and the research page still promises that an experiment that failed stays on the page. The seven entries were mostly copy-editing notes and did not deserve the colophon, but a corrections record for substantive changes (a figure, a status, a claim) is the mechanism that makes the claims register mean something. Keep a short one on the research page, limited to corrections of substance.
5. **The run command differs from the README's.** Both the home and the build page print a command that needs the GitHub CLI and clones the default branch; the README's one-liner uses plain `git` and pins `PUBLISHED_VERSION`. The build page even tells the reader to prefer the README's pinned tag. Print the README's command.
6. **Two organisations in the first hundred pixels.** The wordmark, running head, and footer heading say "CIRWEL Research"; the page title, `og:site_name`, and JSON-LD say "CIRWEL Systems". The footer explains the relationship in a sentence, but a visitor meets both names before that. Pick the one on the wordmark for the title and cards; mention the other once, in the colophon.
7. **Three front doors for one product.** cirwel.org (cream, engraved), cirwel.github.io/unitares (dark, GitHub-styled, different tagline, different navigation), and the README. The site links to the Pages glossary, so visitors do cross between registers. `docs/operations/public-site.md` proposes a `unitares.cirwel.org` alias, which would make a fourth host. Either restyle the Pages build to the house tokens, as cirwel.github.io already does, or move the glossary into cirwel.org.
8. **Smaller items.** "Redis for transient state" on the ecosystem page understates it; Redis is the primary live session store in practice (`CLAUDE.md`). `legacy/react-vite/` carries about five megabytes of retired build output that nothing serves. The figures `fig-1` to `fig-3` and `frontispiece` under `public/sketches/` have been unused since #35.

## 4. Language

### Measured

| Page | Words | Negation markers per 100 words | Self-references |
|---|---|---|---|
| home | 613 | 2.0 | 2 |
| build | 1,067 | 3.0 | 8 |
| ecosystem | 671 | 2.1 | 3 |
| research | 950 | 4.2 | 2 |

Negation markers: "not", "rather than", "never", "cannot", "without", "no", "nothing". Self-references: "this site", "this page", "the build page" and similar.

### The hedge tic

The site's honesty is its best quality and should not be reduced. The form is the problem: almost every claim carries its own disclaimer in the same sentence, usually as "X, not Y" or "X rather than Y".

- "That is evidence that the mechanisms are exercised under real use, not evidence that they outperform an alternative."
- "This is an instrument under evaluation, not a claim that the system can read a model's hidden thoughts or reliably predict failure."
- "These are records from CIRWEL's own development environment, not external adoption or evidence of improved outcomes."

Read in sequence, the reader learns to skip the second half of every sentence, which is the half that matters. Rule: each limit is stated once, on the page that owns it (build §05 and the research page), and product pages state facts without inline disclaimers. The "X, not Y" construction at most once per section.

### Self-reference and voice

The build page talks about itself as a document: "This page is the part you type", "the strongest one on this site because it is the only one that says you never have to touch our code", "the page that says otherwise is selling something". This is chat register and editorializing; cut it. Voice also switches by page: "I" on research, "our repository" and "our code" on build, "CIRWEL" on home. Keep "I" on the research page, where a single named researcher is the point, and make the product pages impersonal.

### Internal vocabulary

Terms that reach the front door without a definition in the same sentence:

| Term | Where | Plain alternative, or define inline |
|---|---|---|
| userland | ecosystem (5), build | "an optional agent runtime built on the public SDK" |
| resident | ecosystem (5), build (3) | "a long-running agent" |
| lease plane, leases | build, home | "exclusive access to one surface at a time, with handoff" |
| attestation, continuity credential | build (3), research (2) | keep on research; on build say "a single-use proof of identity per request" |
| governed surface | build, ecosystem | "a write the server can refuse" |
| verdict | build, ecosystem | "the policy action" (the site already uses "action, reason, next step") |
| receipts, data plate, impression, thesis, volume | navigation, labels | print-trade metaphor leaking into UI labels; see §5 |

The model to follow is already on the ecosystem page: "Names are labels. Identity is bound to the process that acted." A technical noun is fine when the sentence that introduces it says what it does.

### "Agent" now includes people

#46 redefined the word: "agents, human and software, doing, directing, or evaluating long-running work" (home), "Operators and users are agents too" (ecosystem), "accountable infrastructure for agents: people who use or operate the system, and software that acts within it" (footer). Whatever the ontological case, on a front door this asks the visitor to unlearn the ordinary meaning of the site's most frequent word, and it makes the headline "Agent continuity" ambiguous. The research page already uses "principals" for the human-or-software case. Reserve "agent" for software on the product pages, and say "people and agents" where both are meant. This is an operator decision; the cost is stated here so it is a decision and not a default.

## 5. Design

### What works and should not be touched

The engraved register is distinctive and coherent: Bodoni Moda display over EB Garamond text with JetBrains Mono for data, hairlines instead of cards, one accent per impression. The night impression (verdigris rather than a lifted red) is a real design decision with its measurements recorded in `global.css`. Print styles, reduced-motion handling, no-JS fallbacks, the running head, and the token remapping of inline SVG colours are all better than most product sites manage. `fig-1` (the measurement gap) and `fig-2` (heterogeneous agent classes) are genuinely explanatory diagrams.

### What to change

1. **The home page has no picture.** It is 5,500 px at desktop width (7,755 px on a phone) of text with one number plate. The federation claim, many independent runtimes sharing one record, is the most drawable claim on the site, and `frontispiece.svg` is already most of that drawing: five agent classes around one central device. Give it a caption that states the sentence from §1 and put it under the hero. Retire `fig-3` (the verdict loop) from consideration for the home page; it belongs to the build page.
2. **Every page has the same shape.** Hero, receipts, five or six numbered sections each with a medallion, a section rule, a drop cap, then a tailpiece. The eye cannot tell the home page from the ecosystem page. The numbered-section apparatus costs roughly 400 px per seam. Keep it for the long-form pages (build, research), where "a printed volume" is the right metaphor, and give the home page at most four blocks: hero with figure, the five questions, one proof strip, one way in.
3. **Ornament budget.** The house rule is that ornament must be load-bearing. On the home page the fact "running since November 2025" appears in the receipts row, engraved on the seal, and again in §04. The medallion repeats the §NN label beside it. The "Data plate" marker labels a thing that is visibly a data plate. Cut the seal or cut the receipts row, not both; drop the marker.
4. **The way in is the least visible thing on the screen.** The three links under the hero ("Run UNITARES", "See how it fits together", "Read the research") are 12 px uppercase mono at 0.16 em tracking, the same class as every label on the page. One of them should be set as a real call to action in the display face, and the CTA labels should not be uppercase.
5. **Label sizes.** `.label` is 0.72 rem at a 17 px root (12.2 px); the masthead is 10.5 px; the plate marker is 9.9 px. Uppercase tracked mono at 12 px is below comfortable reading size and it is the class used for navigation. Raise labels to 0.8 rem.
6. **Metaphor in the controls.** The theme control reads "AUTO" beside a half-moon and its accessible name is "Impression follows your system". Navigation says "Thesis" for the problem statement; the 404 page says "Not in this volume"; the credentials row says "The receipts". Keep the metaphor in the visual register and make every label literal: "Theme: auto", "Problem", "Not found".
7. **Mobile first screen.** At 390 px the masthead line wraps to two lines and the navigation is eight items in three rows before any content. Collapse the navigation to four items on small screens (Build, Research, Ecosystem, Contact) and drop the masthead line there.
8. **Grain overlay.** `body::before` is a fixed, full-viewport, blended layer above the content. It is subtle, but it sits over the 12 px labels and it repaints on scroll. Move the grain to the body background so it sits under the ink.

## 6. The editorial contract

The site has been rewritten by many sessions: #35 (make the home page legible outside the UNITARES context), #46 (redefine agent), #47 (redesign, 1,675 lines removed), #48 (restore, 1,541 lines added) within nine days, and `docs/assets/README.md` records four hero banners dying between April and September when the sentence baked into them moved. Each session was individually reasonable. The churn comes from having no shared definition of "effective", so each writer supplies one.

The proposal is an `EDITORIAL.md` in `cirwel-site`, read by every session before it edits a page, mirroring how `AGENTS.md` and `CLAUDE.md` share one contract block in this repository. Contents:

1. **The sentence.** The canonical sentence, quoted, with its source in `unitares`. Every surface copies it verbatim; `check-claims.py` pins it.
2. **The spine.** The five questions from `docs/PRODUCT_DEFINITION.md` are the only permitted "what it does" structure on the home and ecosystem pages.
3. **Vocabulary.** Words allowed on the front door without definition (agent, operator, record, evidence, review, outcome, handoff, restart, process). Words that must be defined in the sentence that introduces them (lineage, provenance, attestation, lease, harness, kernel). Words that stay off the product pages (userland, resident, governed surface, verdict, proprioception, EISV).
4. **The hedge budget.** Each limit stated once, on the page that owns it. No per-sentence disclaimers on product pages. "X, not Y" at most once per section.
5. **Voice.** Product pages impersonal. Research page signed "I". No "this site" or "this page". No editorializing about other products.
6. **Design register.** The tokens and faces stay. Ornament budget per page: one figure plate, one section rule per seam, no fact rendered twice. Labels literal.
7. **Corrections.** A change to a figure, a status, or a claim gets an entry in a corrections record and a claims-register update in the same PR.

## 7. Suggested order of work

Small PRs, each reviewable in minutes. Items 1 to 5 are mechanical and can go first; 6 needs the operator's sentence; 7 to 10 are editorial.

1. `cirwel-site`: replace the `GOVERNANCE_TOOL_MODE` paragraph on the build page.
2. `cirwel-site`: re-pin or delete the eight stale claims-register entries; add entries for the pre-registered read on research and for "since November 2025".
3. `cirwel-site`: print the README's pinned run command on both pages.
4. `cirwel-site`: delete `legacy/react-vite/`.
5. `cirwel-site`: one organisation name in the title, `og:site_name`, and JSON-LD.
6. `unitares`: choose the sentence; put it in one place (a constant that `render_social_preview.py`, `build_public_site.py`, and the README all read, or a documented string the README owns) and align `README.md`, `docs/public-site/index.md`, and the glossary intro to it. Then `cirwel-site`: hero, `<title>`, JSON-LD, `og:image:alt`, and a regenerated share card from the same string; pin it in `claims.json`.
7. `cirwel-site`: home page to four blocks, with the frontispiece as the federation plate and the five questions as the spine.
8. `cirwel-site`: hedge and self-reference pass on build and research; vocabulary pass on ecosystem.
9. `cirwel-site`: `EDITORIAL.md`; link it from the repository README and from `docs/operations/public-site.md` here.
10. Decide the Pages landing: restyle to the house tokens or fold the glossary into cirwel.org.

Decisions reserved for the operator, because they change what the site claims rather than how it says it: the sentence; whether "agent" includes people on the product pages; "CIRWEL Research" or "CIRWEL Systems" as the site's name; whether a corrections record returns; which of the three front doors survives.

## 8. Sample rewrite: the home page hero

Before (85 words):

> Agent continuity. Long-running agents restart, lose context, hand work off, and accumulate claims and decisions over time. UNITARES keeps identity, evidence, memory, runtime state, and coordination tied to one accountable record across those boundaries. It runs alongside model providers and agent frameworks rather than replacing them. The goal is simple: make it easier to tell who did what, what evidence supported it, and what should happen next when the work lasts longer than one prompt or process.

After (63 words):

> Many agents, one record. UNITARES is a self-hosted record that independent AI agents share: who did what, what evidence backed it, who reviewed it, and what happened next, kept across the restarts and handoffs that the agents themselves do not survive. Claude Code, Codex, and custom runtimes keep their own models and tools. UNITARES keeps the record. It runs beside evals, guardrails, and sandboxes and replaces none of them.

Nothing technical was removed. The noun list became the four things a reader can check; "runs alongside model providers and agent frameworks" became the named things it runs beside; the two hedges became one boundary sentence at the end.

## Appendix: reproduction

- Build and screenshot: `npm ci && npx astro build` in `cirwel-site`, serve `dist/`, screenshot with headless Chromium at 1440 and 390 px with `prefers-color-scheme` light and dark and `prefers-reduced-motion: reduce`.
- Copy measurements: strip frontmatter, tags, comments, and Astro expressions from each page; count words, sentences, the negation regexes above, and the self-reference regexes above.
- Claims replay: load `src/data/claims.json`, map each source URL to the same path in a local `unitares` checkout, assert each `must_contain` string; assert each `site_says` string against the stripped page text. Sources all matched; eight `site_says` strings did not (listed in §3). The `unitares-resident` README was not available locally and its entry is unverified.
- Not verified here: the deployed site (not fetched), the claim that the governance plugin is TypeScript (JSON-LD), and the `unitares-resident` status line.
