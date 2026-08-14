"""web-drive engine — deterministic site mapping + capability verification.

The engine is the "hands" of the web-drive skill: it crawls a site's route
graph, reads each page's declared surface, drives candidate interactions to
prove they work, and renders the generated per-site driver. It contains no AI
and names no capabilities — that is the agent's job (see ../SKILL.md and
docs/WEB_DRIVE_SPECIFICATION.md).

DELIBERATE DIVERGENCE (spec section 4.3): this file, `cli.py`, and the
web-drive-specific modules are the only engine files that differ from web-qa's.
Every other module here (`browser.py`, `models.py`, `flow.py`, `evidence.py`,
`gate.py`, `accessibility.py`) is a byte-identical copy, so `diff` against
web-qa's engine stays the porting tool. Do not tidy them locally.
"""

__version__ = "0.1.0-dev"
