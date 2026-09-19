---
name: web-drive
description: Turns a live web app into a command-line tool an agent can operate. Explores a site in a headless browser, reconciles what each page *claims* (labels, help text, form validation) against what navigation *proves* (real routes, auth gates, server-side rejections), verifies each candidate action by actually performing it, then generates a per-site CLI with named verbs, --help, JSON output and real exit codes — so a later session completes real tasks on that site from the terminal without re-exploring it. Use when asked to map a site for automation, generate a CLI/driver/wrapper for a web app, make a site scriptable or agent-operable, or produce an operating manual for a web app. Sibling of web-qa (is it broken?) and web-replicate (how do I rebuild it?); this one answers: how do I operate it? Runs inside a Claude Code session; needs no API key.
---

# web-drive

> **Status: v1 complete (Phases A–H shipped, spec frozen at v1.4).** The engine
> ships `map`, `read`, `probe`, `verify`, `extract` and `generate`, all
> schema-frozen and test-guarded. Capability inference (§3 below) is a
> **deliberate, permanent design choice, not an unfinished phase** — `generate`
> renders a driver FROM a catalog you assemble by hand per §3; it does not
> infer one, and never will, by design (the engine/agent split this whole
> family is built on). See `docs/WEB_DRIVE_SPECIFICATION.md` and `TODO.md`.

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

A persistent app shell (a header, a theme switcher, an AI-assistant toggle)
renders the SAME button on every route, so probing it once per template still
re-pays the full reload cost once per *different* template — measured live on
content-jumpstart.com (2026-09-16): 5 shell buttons across 19 distinct templates
cost ~76 redundant reloads for zero new routes, most of them toggles that never
navigate at all. Once a button's label has produced the SAME outcome (a
destination, or "clicked, nothing navigated") on two *different* templates, `map`
trusts it and stops re-clicking it — freeing its `--max-probes` slot for a
page-specific control instead. A label whose outcome differs between templates
is never cached and is always re-probed, so this never costs coverage; the
output's `probe_cache_hits` field says how many clicks this run actually skipped.

**A same-URL multi-step flow (a wizard) needs a manual look — `map` only leaves
you a lead, on purpose.** Every check in this engine keys off URL equality, so a
button that advances a flow WITHOUT changing the URL (content-jumpstart.com's
Project Wizard advances Client → Research → Templates → … entirely on
`/dashboard/wizard`) used to be completely invisible — indistinguishable from a
dead toggle, in every part of the crawl. Each route in `sitemap.json` now also
carries `state_changing_controls` (a button whose click left the URL unchanged
but altered the page's own content — read this as "there's a flow here, go look
by hand") and `gated_controls` (a button whose click failed because Playwright
found it present but disabled — read this as "likely gated behind page state
this isolated probe never provided, e.g. an empty combobox"). Neither list is
auto-followed: `map` never fills a combobox or retries a gated control to get
past it, because that would mean guessing valid business data and chaining
through a *mutating* flow with no agent judgment in the loop — content-jumpstart's
own `wizard advance` capability creates a real project as a side effect. Treat
both lists as candidates for the SAME manual-inspection-then-`verify` procedure
this section already describes for any other capability, not as a phase to
automate further.

**Two caps stay on, and neither limits reachability.** They bound *effort per
unit of coverage*, which is the opposite thing — removing them shrinks the map:

- `--max-probes` (12) — button probes per route. Unlimited, a 146-control page
  spends ~650 requests on itself and breadth-first never leaves depth 1
  (measured: 13 routes uncapped vs 20 at 8). The budget is spent in DOM order
  with no ranking pass, so a page whose primary CTA renders last (React Native
  Web ties every control at one rank, or the control just sits after a long
  app-shell chrome list) can be starved before the crawl ever reaches it.
  Every route now discloses `controls_probed`/`controls_skipped_budget`, so
  "we clicked 12 of 63 probe-safe buttons" is a fact you can read off the map
  instead of infer — a route with a nonzero `controls_skipped_budget` is worth
  a manual `read`/`probe` pass on, the same way a `capped` map is.
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

**A form-less page still gets a `forms[]` entry if it has real fields.** Many
modern multi-step wizards are a controlled-component tree with its own submit
handler, not a native `<form>` (quizsquirrel.com's `/quiz/create`, found
live). When the page has zero `<form>` elements but has visible input/select/
textarea fields, `read` falls back to ONE whole-document group flagged
`"implicit": true` — that flag means the group's `selector` (`"body"`) is not
a real submit boundary, unlike an ordinary form capture; treat it as "here are
the fields and a best-guess submit control", not as one cohesive form to
submit blind.

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
  navigates somewhere sharing none of the label's significant words). A
  control that is `disabled` at the time of the click is skipped with no
  finding at all, never reported `advertised_absent` — a disabled-by-design
  control (a wizard step gated behind the prior one, a submit gated behind
  required fields) isn't claiming to do anything yet, so there's nothing to
  disprove; naming what it's gated on, if worth doing, is your call to make
  by hand, same as `map`'s `gated_controls`;
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

`generate` (§6 below) renders a driver from a catalog — it does not build the
catalog itself. This section is **your** procedure for turning `sitemap.json`
+ `surface.json` + `reconciliation.json` into the `capabilities[]` entries
spec §5 describes, by hand. That split is permanent, not a placeholder for a
future phase: engine output is evidence; naming, classifying and writing
assertions is judgment, and judgment stays yours.

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
good as the evidence that produced it. `dom_contains` matches against the
`dom_outline` role/text tree of *interactive + landmark* nodes, not rendered
body text — a page whose result is plain text (a confirmation, an error, a
bare heading) has an EMPTY outline and every `dom_contains` against it fails
even though the text is right there on the page. Use `content_contains` for a
text outcome; reserve `dom_contains` for asserting a control or landmark
exists.

**Watch for a consent/overlay banner eating the FIRST click of a capability.**
Found live: a cookie-consent backdrop sitting on top of a login form ate a
`{"type":"click","selector":"button[type='submit']"}` step with EVERY gate
check green — the click landed on the banner, not the form, so `verify`
reported success while the URL never left `/login` and the step's `http[]`
was empty. Nothing in the deterministic gate catches this (a click that
"succeeds" against the wrong element looks identical to one that succeeds
against the right one). Before authoring a capability's first interactive
step, `read` the page and check for a `role=dialog`/consent-banner-shaped
control; if one exists, add a step to dismiss it (e.g.
`{"type":"click","selector":"text=Accept All"}`) BEFORE the real step. A step
whose gate passed but produced zero URL change and zero network activity is
worth treating as suspect for this reason even without a banner in evidence.

### 3a. Driving a gated/stateful flow interactively — an alternative to guessing steps from markup

A `state_changing_controls`/`gated_controls` entry (the "Map the route graph"
step above, v1.6) names a lead,
not an answer: a button changed the page without navigating, or failed to
click because something else on the page has to happen first. The default
way to turn that lead into `steps[]` is to read the page's HTML and write a
guess, then bet on `verify` passing in one shot — workable, but a same-URL
wizard with several sequential steps (content-jumpstart.com's Project Wizard:
Client → Research → Templates → Quality Gate → Export) means several guesses
compound, and a wrong one three steps in gives no signal about which step was
wrong.

`interact` is the other option: a real browser that stays open across
SEPARATE CLI calls, so you click one thing, see the ACTUAL result, and decide
the next click — the same manual process, but against a live page instead of
static markup, and with ground truth after every step instead of only at the
end.

```bash
python -m engine.cli interact start --url <URL> --state session.json \
    [--session <auth-bundle>] [--headless]
python -m engine.cli interact read  --state session.json
python -m engine.cli interact click --state session.json --text "Continue to Research"
python -m engine.cli interact fill  --state session.json --selector "#foo" --value "bar"
python -m engine.cli interact stop  --state session.json
```

Each call is a separate process; `--state` is the handle connecting them (one
call per `--state` path — reuse it for every action against the same
session, and always `stop` when done, or the detached chromium process leaks).
`click --text "..."` matches the first element containing that visible text
(how a human would refer to a control); `--selector` targets precisely when
text is ambiguous. `read`'s `content_preview` and `click`'s `changed`/
`navigated` fields are your only feedback — there is no snapshot/DOM dump, on
purpose: this is a discovery aid for finding the real sequence, not a
replacement for `read`/`probe`.

**`changed: false` is not proof nothing happened.** Confirmed live against
content-jumpstart.com's own wizard: opening its client combobox reported
`changed: false` (the click's own 400ms settle wasn't enough for that
dropdown's render), yet a separate `read` immediately afterward clearly
showed it open. Treat `changed`/`navigated` as a fast hint, not an answer —
call `read` whenever it actually matters whether something happened.

**No `--yes` gate, no auto-anything, and that's deliberate.** `interact`
guesses nothing (no plausible values, no chaining through a flow on its own)
and blocks nothing (no destructive-label filtering, no cost awareness) —
every single click is a call YOU chose to make. The backstop is the same one
`verify --destructive`/`--costs` already leans on: the session's own
permission prompt over each Bash-level `interact` invocation. Never script an
unattended sequence of `interact` calls against destructive-looking or
costed-looking controls — do it the same deliberate, one-call-at-a-time way
you would while actually watching.

**Once the real sequence is known, still go through `verify`.** `interact`
finding that "select the combobox, then click Continue" works is not itself
verification — write the equivalent `steps.json` and run `verify` (§4) to get
the deterministic gate, the `assert`, and (for anything destructive or
costed) the explicit `--destructive`/`--costs --yes` confirmation. `interact`
is how you FIND the steps; `verify` is still what proves them.

### 4. Verify a candidate — execute it, don't guess

```bash
python -m engine.cli verify --url <URL> --verb "quiz list" \
    --steps steps.json --assert assert.json [--destructive --costs --yes] \
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

**Pass `--output` whenever you'll judge the produced content afterward.**
Each step's `content_after` can run to 20,000 chars (BUGS.md 2026-08-21) — on
a multi-step capability that alone can dominate your context budget for no
benefit once the full text is on disk. Passing `--output <file>` makes
stdout show only a ~500-char excerpt per step (with a `[stdout truncated:
showing N of M chars — full content in <file>]` marker); the file always
holds the untruncated result. Omit `--output` and stdout stays full-length
exactly as before. Read the file when the excerpt isn't enough to judge the
outcome.

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

**`--costs` is the same gate for a verb that spends real credits or
third-party money WITHOUT being destructive** (a paid research tool, a paid
generation's "Confirm & start") — content-jumpstart.com's `research run`
(200 credits) is exactly this shape, and before v1.7 it had zero engine-level
protection: nothing but your own judgment stood between running it and a real
charge. Declare it with `--costs`, same refuse-without-`--yes` mechanics as
`--destructive`, and record what it actually costs in the capability's
`"costs"` field (e.g. `{"credits": 200}`) once verified — a future run of
`verify`/the generated driver refuses it exactly like a destructive verb
until the same explicit confirmation is given again.

**Record the proof in the catalog, not just in your own head.** When a
`verify` run passes, always pass `--output <path>` and carry that path
forward into the capability entry you're assembling per §3: set
`"verified_at"` to the run's own timestamp and `"evidence"` to the `--output`
path. `generate` (§6) copies and redacts that file into the driver's own
`runs/` and repoints `evidence` at the copy — but only if you gave it
something to copy. A capability with no `evidence` field still ships (nothing
requires it), it just carries no durable proof past this session.

Also worth carrying into the assembled catalog: the full route list from your
`map` run, as a top-level `"routes"` array (spec §5) — every route `map`
discovered, not just the ones that became capabilities. This is
documentation, not required for `generate` to work, but it's the difference
between a driver whose manual says "here's what you can do" and one that also
says "here's everything this app has, including what isn't wired up yet."

### 5. Extract structured records — the read-verb backbone

```bash
python -m engine.cli extract --url <URL> --spec extract-spec.json --output records.json
```

`extract-spec.json` is exactly spec §5's `extract` key:

```jsonc
{
  "container": {"role": "listitem"},
  "fields": {
    "id":      {"attr": "data-id"},
    "title":   {"role": "heading"},
    "updated": {"selector": "time", "attr": "datetime"}
  }
}
```

`container` finds every repeated item on the page (`selector` wins if given,
else `role` — resolved against an explicit `role=""` first, an implicit-role
table second: `<li>`→listitem, `<article>`→article, `<tr>`→row, `<h1>`–`<h6>`→
heading, and so on). Each `fields` entry finds ONE value inside that item the
same way (`selector`/`role`, or neither for the container element itself),
then reads an `attr` if given, else trimmed text content. Deterministic and
literal on purpose — it does not infer a container or guess field names; that
judgment already happened when you wrote the spec from what `read` told you
about the page (§1's `controls`/`landmarks` are exactly where a sensible
`container`/field selector comes from).

This is the mechanism a `kind: "read"` capability's `--json` output runs on —
`quiz list --json` is `extract` against `/quizzes` with the spec above,
wrapped in the generated driver's command tree (§6 below).

### 6. Generate the driver

```bash
python -m engine.cli generate --catalog site.json --out ../../../drivers/<slug>
```

`site.json` is the catalog you assembled per §3 — spec §5's schema exactly:
`site`, `auth`, `routes`, `capabilities[]` (each with `steps`/`assert`, and
`extract` for read verbs), `unverified[]`, `reconciliation[]`. `generate`
does not infer, rank or verify anything; it only renders. Writes
`drivers/<slug>/`:

- `site.json` — the catalog, verbatim, **except** each capability's own
  `evidence` field: if it named a real file, that file gets copied (redacted)
  into `runs/` and this field is repointed at the copy (below).
- `SITEGUIDE.md` — a human-facing manual: every capability with its params
  table, destructive verbs flagged, and `unverified[]` entries listed with
  their withholding reason (so a human sees *why* a verb is missing, not just
  its absence).
- `runs/<verb-slug>.json` — for every capability whose `evidence` field
  pointed at a real file: a sanitized copy of that `verify` result. Every
  step's typed-in `value`/`text` is replaced with a redaction placeholder
  first, unconditionally — a resolved `{"env": "VAR"}` secret is a literal
  value by the time `verify` records it, and nothing upstream redacts it, so
  an unredacted copy would ship a real password (or just a real typed name)
  inside a driver meant to be handed to someone else. What survives is what
  actually proves the verb ran: selectors, HTTP status/method, gate checks,
  assertions. A capability with no `evidence` field, or one pointing at a
  file that isn't there, gets nothing written — `generate` reports the miss
  rather than pretending it copied something.
- `<slug>` (POSIX shim) and `<slug>.cmd` (Windows wrapper) — both load the
  SAME `_engine/runtime.py`.
- `_engine/` — `runtime.py` and its dependency closure (`browser`, `models`,
  `accessibility`, `evidence`, `gate`, `flow`, `verify`, `extract`) copied
  BYTE-IDENTICAL from this skill's own engine. This is what makes
  `drivers/<slug>/` standalone: **a cold shell with only that directory,
  no web-drive skill installed, can run every capability** (spec §12).

The generated driver's command tree is `<slug> <noun> <verb-part> [flags]`
(a capability's `verb` "quiz list" becomes `quiz list`, a `noun` group with
a `list` command inside it), plus built-ins `capabilities` (machine-readable
catalog), `doctor` (re-check the site's title against the stored
`fingerprint`; exit 3 on drift, never auto-regenerates — report, don't act),
and `manual` (prints `SITEGUIDE.md`). Every verb command accepts `--json`,
`--yes` (required for a `destructive: true` verb — refuses with exit 4
otherwise, spec §7's permission gate, no engine allowlist), `--dry-run`
(prints the steps WITH THIS INVOCATION'S FLAGS RESOLVED — `content-jumpstart
client create --name "Acme" --dry-run` shows `"value": "Acme"`, not the
catalog's raw `${name}` template — without touching the browser), `--session`,
and `--timeout`. Exit codes: 0 verified, 1 ran-but-not-verified, 2 precondition
failed (no `--session` where a capability needs `auth`), 3 drift (a step's
own locator wasn't found — this is how a redeployed site's selector-rot
becomes visible instead of a confusing assertion failure), 4 refused.

CLI params substitute into a step's `${NAME}` references the same way a
step's `{"env": "VAR"}` secrets already do (`flow.resolve_str`, reused
verbatim) — write a capability's `steps` with `${title}` etc. and the
generated `quiz create --title "Trees"` fills it in at runtime.
