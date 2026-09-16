---
name: web-drive
description: Turns a live web app into a command-line tool an agent can operate. Explores a site in a headless browser, reconciles what each page *claims* (labels, help text, form validation) against what navigation *proves* (real routes, auth gates, server-side rejections), verifies each candidate action by actually performing it, then generates a per-site CLI with named verbs, --help, JSON output and real exit codes — so a later session completes real tasks on that site from the terminal without re-exploring it. Use when asked to map a site for automation, generate a CLI/driver/wrapper for a web app, make a site scriptable or agent-operable, or produce an operating manual for a web app. Sibling of web-qa (is it broken?) and web-replicate (how do I rebuild it?); this one answers: how do I operate it? Runs inside a Claude Code session; needs no API key.
---

# web-drive

> **Status: Phase E.** The engine ships `map`, `read`, `probe` and `verify`;
> capability inference (§3 below) is agent procedure, not engine code yet —
> there is no `generate` to automate it. Result extraction (§F) and driver
> generation (§G) are not built yet — see `docs/WEB_DRIVE_SPECIFICATION.md`
> and `TODO.md`.

## Mission — make the app operable, not just understood

You are the reasoning half of a system that **turns a website into a CLI**. A
deterministic Python engine under `engine/` is your hands: it crawls routes,
reads page surfaces, drives interactions, and renders the driver. It never
decides what anything is *for* — **you** name capabilities in human terms,
derive their parameters, classify their risk, and decide what counts as success.

The output is not a document. It is `drivers/<site-slug>/` — an executable
shim, a `site.json` capability catalog, and a `SITEGUIDE.md`. A later session
runs `<site-slug> capabilities --json`, then `<site-slug> quiz create --title X`,
and never touches a selector.

## The rule that defines this skill

**Build from two sources and treat their disagreement as output.**

- **Read** what the app *claims*: nav labels, headings, button text, form labels
  and `aria`, placeholder/help text, validation attributes, empty-state copy.
- **Navigate** to find what it *proves*: where a link actually lands, which
  routes silently redirect to login, real URL patterns behind pretty labels,
  which fields the server rejects regardless of what the markup said.

Where they disagree, record it in `reconciliation[]` **and change the catalog** —
a field the markup calls optional becomes a required flag if navigation proves
the submit fails without it. Reading alone is a sitemap scraper; clicking alone
is a macro recorder.

## Verify or withhold

**A capability becomes a CLI verb only if it was executed successfully during
generation, with its success signal captured.** Everything else goes to
`unverified[]` with a reason — documented in the manual, never runnable. Same
discipline as web-qa's gate: publish nothing that was not demonstrated.

## Safety

Destructive candidates are yours to classify — there is no engine allowlist.
Verifying a mutating verb means really performing it, so it requires explicit
user confirmation through the session's permission prompt, and is **never** run
against a target the user does not own (it ships `unverified` instead). The
generated driver carries that classification forward: `destructive: true` verbs
refuse to run without `--yes`.

## Setup (once)

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Authentication, including logins you cannot script

Most apps are behind a login, and some of those logins **cannot be driven** —
Google/SSO actively blocks automated browsers, and passkeys and MFA are designed
to resist exactly this. Do not try to defeat that; it is an arms race a QA tool
should not enter, and a tool that fakes its way past a security control is worse
than one that admits the boundary.

Instead, sign in **once, by hand**, and reuse the result:

```bash
# Opens a REAL browser. You complete OAuth/SSO/MFA yourself.
python -m engine.cli login \n    --url https://app.example.com/login \n    --until-url /dashboard \n    --save-session .qa/session.json \n    --user-agent "Mozilla/5.0 ... QASession"

# Every later run is headless and authenticated.
python -m engine.cli map \n    --url https://app.example.com/dashboard \n    --session .qa/session.json \n    --user-agent "Mozilla/5.0 ... QASession"
```

What is saved is the **app's** session (cookies + localStorage), not the identity
provider's. Once the redirect completes the provider is out of the picture, which
is why one manual sign-in unlocks every later headless run until the token
expires. Use `--until-selector` instead of `--until-url` when the app lands back
on the same path.

**Pin the same `--user-agent` for the login and every replay.** Tokens are
commonly bound to a UA+IP fingerprint, so a bundle saved under one UA and
replayed under another is rejected — and it fails looking like an expired
session, which sends you hunting the wrong problem.

The bundle format is shared with `web-qa`, so a session established by either
skill is replayable by the other.

## Workflow

### 0. Map the route graph

```bash
python -m engine.cli map --url <URL> --probe-buttons --fill-forms --output sitemap.json
```

Breadth-first from the entry URL, same-origin only, recording for each route
whether it was reachable, what it redirected to, and whether it required auth.
Pass `--session <bundle>` to map authenticated routes (same session-bundle
format as web-qa's `flow --save-session`).

**Traversal is exhaustive by default.** There is no page cap, no depth cap and
no per-template sampling unless you ask for one. Only three things bound a
crawl:

- **same origin** — off-origin links are recorded under `skipped`, never followed;
- **visit once** — every URL is normalized and fetched at most once;
- **`--time-budget-s`** (default 3600) — wall clock for the whole run, including
  auto-resume legs.

That ordering is deliberate. A page or depth cap decides *in advance* which
parts of a site matter, and it does so silently — which is how a map comes back
looking complete while describing a fraction of an app. A clock bounds cost
without pre-judging coverage, and it is recoverable: the frontier travels in the
output, so `--resume <sitemap.json>` continues from exactly where it stopped.

**Always read the three trust flags before believing a map.** `capped`,
`timed_out` and `rate_limited` each mean "this is partial", and each leaves the
frontier intact for a resume. A map with `frontier: []` and all three false is
the only one that claims the site was actually exhausted.

`--probe-buttons` is what reaches an SPA's real surface — on a button-routed app
the link crawler finds almost nothing (isekaizero: 16 of 20 routes came from
buttons, and its entire app shell was unreachable by `<a href>`). It costs a page
load per candidate, so it is the main driver of runtime. `--fill-forms` reaches
what sits behind search and filter gates; it never submits a form marked
destructive or one containing a password field.

**Two caps stay on, and neither limits reachability.** They bound *effort per
unit of coverage*, which is the opposite thing — removing them shrinks the map:

- `--max-probes` (12) — button probes per route. Unlimited, a 146-control page
  spends ~650 requests on itself and breadth-first never leaves depth 1
  (measured: 13 routes uncapped vs 20 at 8).
- `--max-query-variants` (3) — distinct query strings walked per path template.
  A faceted browse page is **one route with a parameter space**: every filter
  chip mints a URL, so the space is combinatorial and never converges.
  isekaizero's `/explore` produced 37 variants and consumed a 45-minute budget
  to learn one page shape.

Variants beyond the sample are still counted, and **the parameter vocabulary is
recorded from every variant whether walked or not** — that is the part a driver
can use:

```json
{"template": "/explore", "visited": 3, "variants_seen": 37, "variants_collapsed": 34,
 "params": {"category": {"values": ["romance", "mystery", …], "truncated": false},
            "sortType": {"values": ["trending", "random", "discovery"], "truncated": false}}}
```

`explore --category romance --sortType trending` generalizes; a list of 37 URLs
does not.

The true coverage caps are all opt-in, for a deliberately partial survey:
`--max-pages`, `--max-depth`, `--max-per-template`, `--max-legs`. Each is
disclosed in the output when it binds.

Pacing is not a cap and stays on: `--max-rpm` (default 120) meters *requests*,
the unit limiters actually count, and `--block-assets` (default on, Chromium
only) drops images and media — roughly a 75% request cut. Neither limits what
gets mapped.

### 1. Read a page's declared surface

```bash
python -m engine.cli read --url <URL> --output surface.json
```

Extracts what **one page claims** (spec §3's "claims" half, not yet checked
against navigation — that is Phase C's `probe`): headings, aria landmarks
(role + accessible name), non-form controls addressed role-first, and every
form's field schema — `required`, `maxlength`, `minlength`, `pattern`,
`<select>` options, aria-describedby — plus visible error (`role=alert`/
`aria-live`) and empty-state copy. Pass `--session <bundle>` to read an
authenticated page (same bundle format as `map`).

This is a single-page read with no crawl and no navigation: point it at one
route from a `sitemap.json` you already have. Nothing here is verified yet —
`read` reports what the markup says, not what happens when you act on it.

### 2. Check a claim against what navigation proves

```bash
python -m engine.cli probe --url <URL> [--check-preconditions] --output reconciliation.json
```

Reads the page fresh, then acts on what it found and records what actually
happened — spec §3's "proves" half, run against `read`'s "claims" half:

- clicks every non-form control that doesn't look mutating (same
  `is_probe_safe` skip as `map --probe-buttons` — delete/save/publish/buy
  wording is never clicked here either) and checks `advertised_absent` (the
  click fails, or lands on a >=400 status) and `label_route_mismatch` (it
  navigates somewhere sharing none of the label's significant words);
- submits every non-destructive form once per optional field left blank, to
  check `optional_but_required` (a new error appears that wasn't there
  before);
- with `--check-preconditions` (off by default — a second browser launch per
  navigating control), re-opens a control's landing URL in a FRESH context
  seeded with the same auth but none of the client-side state the click-through
  accumulated, to check `undocumented_precondition`.

Findings are facts (`reconciliation[]`), not verdicts — naming the verb a
finding should change, or deciding it doesn't matter, is Phase D's job, not
this command's. `probe` performs real clicks and real form submissions
against whatever `--url` points at; point it at a target you own, the same
way `map --probe-buttons`/`--fill-forms` do.

### 3. Infer capabilities from map + read + probe output

There is no `generate` command yet (Phase G) — this section is **your**
procedure for turning `sitemap.json` + `surface.json` + `reconciliation.json`
into the `capabilities[]` entries spec §5 describes, by hand, until the engine
can do it for you. Engine output is evidence; naming, classifying and writing
assertions is judgment, and judgment is yours.

**Noun-verb naming.** `<noun> <verb>`, both lowercase, matching the domain the
*app* uses — read its own nav labels and headings before inventing terms. A
route's primary form or its dominant read content usually names the noun
(`/quizzes` → `quiz`); the action names the verb (`list`, `create`, `delete`,
`update`). Prefer the site's own word for the noun over a generic one — `quiz`
on a quiz app, not `item`.

**Classification — `kind` and `destructive`.** A route with no mutating form
and a GET-shaped purpose is `kind: "read"`. A route whose primary action is a
form submit is `kind: "mutating"`. `destructive` starts from `read`'s own
per-form heuristic (password-outside-login, or pay/delete/subscribe/sign-up
wording) — but **that heuristic is scoped to the submit control's own text and
field labels, not the whole form's `innerText`**, precisely because a form
commonly nests an unrelated cross-link (a login form's "Don't have an account?
Sign up") whose wording must not contaminate what the SUBMIT does. Confirmed
live on quizsquirrel.com's `/login` (2026-09-16): the raw form text read
`"Sign in ... Don't have an account? Sign up"`, which an earlier, whole-text
version of this heuristic misclassified as destructive — an ordinary,
idempotent login would have needed `--yes` for no reason, and `probe`'s
requiredness check would have skipped testing it entirely (destructive forms
are never submitted). Trust the submit button's own words over the page's.

Worked example, from that same page (`read --url https://quizsquirrel.com/login`):

```jsonc
{
  "verb": "auth login",
  "summary": "Sign in with an email and password.",
  "kind": "mutating",
  "destructive": false,                 // submit says "Sign in" — idempotent auth
  "params": [
    {"name": "email",    "type": "string", "required": true},
    {"name": "password", "type": "string", "required": true, "secret": true}
  ]
}
```

against the *same site's* `/register` (submit text "Create account", matching
`register`/`create account` in the destructive vocabulary):

```jsonc
{
  "verb": "auth register",
  "kind": "mutating",
  "destructive": true,                  // creates a new account — not idempotent
  "confirm": true,
  "params": [                            // real fields from /register's surface.json
    {"name": "email",           "type": "string",  "required": true},
    {"name": "username",        "type": "string",  "required": true},
    {"name": "displayName",     "type": "string",  "required": true},
    {"name": "password",        "type": "string",  "required": true, "secret": true},
    {"name": "confirmPassword", "type": "string",  "required": true, "secret": true},
    {"name": "birthYear",       "type": "number",  "required": true},
    {"name": "acceptedTerms",   "type": "boolean", "required": true}
  ]
}
```

**Param derivation.** One `params[]` entry per form field from `read`'s
`fields[]`: `name` from the field's `name`, `type` from a light mapping
(`email`/`text`/`tel`/`url` → `string`, `number` → `number`, `checkbox` →
`boolean`, a `<select>`'s `options` → an enum), `required` from the field's own
`required` **unless** `probe`'s `reconciliation[]` contains an
`optional_but_required` entry naming it — that finding **promotes** the param
to `required: true` regardless of what the markup claimed (spec §3's
"Effect on the catalog" column; this is the one place a `probe` finding directly
edits a capability rather than just informing your read of it). A password
field gets `"secret": true` so a generated driver never echoes or logs it.

**Reading the other three `reconciliation[]` kinds into the catalog:**

- `label_route_mismatch` — document the verb against the route it actually
  lands on, not the label's wording (`quiz view` reachable via a nav item
  labeled "My Stuff" is still named for the noun, not the label).
- `advertised_absent` — withhold the verb. It goes in `unverified[]` with the
  reason (`probe`'s `observed` string), never as a runnable capability.
- `undocumented_precondition` — add a `preconditions[]` entry naming what
  `probe` showed was actually required (e.g. `"onboarding_complete"`), so a
  later session knows to satisfy it before calling the verb, instead of
  discovering the failure at runtime.

**Assertion authoring.** Reuse web-qa's assertion vocabulary verbatim (spec
D6): `url_contains` for a route change, `content_contains` for text that must
appear, `dom_contains` for a selector that must exist. Pick the assertion from
what `probe` actually observed succeeding, not from what seems plausible — the
whole point of verify-or-withhold (Phase E) is that an assertion is only as
good as the evidence that produced it.

### 4. Verify a candidate — execute it, don't guess

```bash
python -m engine.cli verify --url <URL> --verb "quiz list" \
    --steps steps.json --assert assert.json [--destructive --yes] \
    --output verify-result.json
```

`steps.json` is a list of flow-style step objects — the same schema web-qa's
`flow` already runs (spec D6), reused verbatim: `{"type": "click"/"fill"/
"navigate"/..., "selector"?, "value"?, "text"?, "assert"?, "settle_ms"?,
"await_response"?}`. Secrets are referenced, never inlined:
`{"env": "QUIZSQUIRREL_PASSWORD"}` or `${QUIZSQUIRREL_PASSWORD}` inside a
string. `assert.json` is the CAPABILITY-level assertion (the worked example's
`{"content_contains": "..."}` or `{"url_contains": "..."}` etc. — same key
vocabulary as a per-step `assert`), checked against the last step's evidence.

This is the whole point of **verify-or-withhold** (spec §5): a candidate only
becomes a runnable verb if this command actually ran it and it actually
passed — every step's own deterministic gate (console errors, HTTP status,
crash, error page, sane navigation) AND its own `assert`, then the
capability-level `assert` at the end. It halts at the first failing step —
report the `reason` and withhold the verb to `unverified[]`, never publish it
"verified with caveats."

**Destructive candidates require `--destructive --yes`, matching spec §7 —
this is the permission gate, and there is no engine allowlist to bypass it.**
`--destructive` alone refuses (exit 4) without running anything; add `--yes`
only after the session's own permission prompt has approved it, and never
against a target you don't own — ship it as `unverified` with the reason
instead. `verify` performs the real action every time it succeeds: a login
verifies by actually signing in, a delete verifies by actually deleting. Pair
a destructive verb's verification with a cleanup recipe where one exists.

*Phases F–H are not implemented yet.*
