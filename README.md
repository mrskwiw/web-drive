# web-drive

A Claude Code plugin that turns a live web app into a **command-line tool an agent can operate**.

It explores a site in a headless browser, reconciles what each page *claims* (labels, help text, form validation) against what navigation *proves* (real routes, auth gates, server-side rejections), verifies each candidate action by actually performing it, then generates a **per-site CLI** — named verbs, real `--help`, JSON output, real exit codes.

The point: a later session completes real tasks on that site from the terminal without re-exploring it and without touching a selector.

```bash
$ quizsquirrel capabilities --json          # discover what's available
$ quizsquirrel quiz list --json             # read
$ quizsquirrel quiz create --title "Trees"  # write
$ quizsquirrel quiz delete --id 41 --yes    # destructive, gated
```

Third of three siblings — same instrument, different question:

| Skill | Question |
|---|---|
| [`web-qa`](https://github.com/mrskwiw/web-qa) | Is it broken? |
| [`web-replicate`](https://github.com/mrskwiw/web-replicate) | How do I rebuild it? |
| **`web-drive`** | **How do I operate it?** |

## Status — early

**v0.1.0, Phase A.** The engine carries the browser/flow core and the `map` subcommand (route-graph crawl). Verb inference, verification, result extraction and driver generation are specified but not built. The generated-CLI examples above are the target, not yet the behaviour.

## Install

```
/plugin marketplace add mrskwiw/mrskwiw-plugins
/plugin install web-drive@mrskwiw-plugins
```

Then once:

```bash
cd skills/web-drive
pip install -r requirements.txt
python -m playwright install chromium
```

## Two ideas it's built on

**Read *and* navigate, and treat the disagreement as output.** A guide built only from reading a page is a sitemap scraper; one built only from clicking is a macro recorder. Where the two disagree — a field the markup calls optional that the server actually rejects, a nav item labelled "Reports" that lands on `/analytics` — the discrepancy is recorded *and changes the generated CLI*.

**Verify or withhold.** A capability becomes a runnable verb only if it was executed successfully during generation, with its success signal captured. Everything else is documented as `unverified` with a reason, and is not exposed. Nothing ships that wasn't demonstrated.

## Engine subcommands

Run from `skills/web-drive/` as a module (`python -m engine.cli …`):

| Command | Status | Purpose |
|---|---|---|
| `map` | ✅ | Same-origin route-graph BFS → `sitemap.json`: final URL after redirects, status, depth, how it was reached, observed auth state |
| `read` | planned | One page's declared surface: controls, form schemas, copy, aria |
| `probe` | planned | Navigate a candidate transition; feeds reconciliation |
| `verify` | planned | Execute every candidate action; demote failures to `unverified` |
| `extract` | planned | Structured records from listing/detail pages |
| `generate` | planned | Render `drivers/<slug>/` — shim, catalog, manual |

`map` accepts `--session <bundle>` to crawl authenticated routes. Session bundles use the **same format as `web-qa`'s `flow --save-session`**, so a session established by either skill is replayable by the other — authenticating is the expensive, rate-limited step and is worth sharing.

Two habits inherited deliberately: a crawl that hits `--max-pages` sets `capped` in its output, because a truncated map that reads as complete is how a generated driver silently omits half a site; and a crawl run *with* a session marks routes `auth: unknown` rather than `public`, because with a session every route authenticates and `public` would be a guess dressed as an observation.

## Safety

Destructive candidates are classified by the agent, not by an engine allowlist. Verifying a mutating action means really performing it, so it needs explicit confirmation through the session's permission prompt, and is never run against a target you don't own — it ships `unverified` instead. The generated driver carries that forward: `destructive: true` verbs refuse to run without `--yes`.

## Design

Two halves with a JSON seam, like its siblings: a deterministic Python **engine** (the hands — crawls, drives, renders; no AI, names nothing) and the **Claude Code agent** running `SKILL.md` (the reasoning — names capabilities, derives parameters, classifies risk, decides what success means). The seam is `site.json`, the capability catalog.

## Layout

```
.claude-plugin/plugin.json     plugin manifest
skills/web-drive/
├── SKILL.md                   the agent workflow
├── engine/                    deterministic engine
├── requirements.txt           playwright + click
└── tests/                     pytest suite
```

## License

MIT
