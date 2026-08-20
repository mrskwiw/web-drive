---
name: web-drive
description: Turns a live web app into a command-line tool an agent can operate. Explores a site in a headless browser, reconciles what each page *claims* (labels, help text, form validation) against what navigation *proves* (real routes, auth gates, server-side rejections), verifies each candidate action by actually performing it, then generates a per-site CLI with named verbs, --help, JSON output and real exit codes — so a later session completes real tasks on that site from the terminal without re-exploring it. Use when asked to map a site for automation, generate a CLI/driver/wrapper for a web app, make a site scriptable or agent-operable, or produce an operating manual for a web app. Sibling of web-qa (is it broken?) and web-replicate (how do I rebuild it?); this one answers: how do I operate it? Runs inside a Claude Code session; needs no API key.
---

# web-drive

> **Status: Phase A skeleton.** The engine currently carries the copied
> browser/flow/models core and the `map` subcommand. Verb inference (§Phase D),
> verification (§E), extraction (§F) and driver generation (§G) are not built
> yet — see `docs/WEB_DRIVE_SPECIFICATION.md` and `TODO.md`.

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
python -m engine.cli map --url <URL> --output sitemap.json
```

Breadth-first from the entry URL, same-origin only, recording for each route
whether it was reachable, what it redirected to, and whether it required auth.
Pass `--session <bundle>` to map authenticated routes (same session-bundle
format as web-qa's `flow --save-session`).

*Phases B–H are not implemented yet.*
