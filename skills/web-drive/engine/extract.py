"""``extract`` — Phase F: container/field specs → structured records.

The backbone of a read verb (spec §5's `extract` key on a capability): given
a spec naming how to find repeated items on the CURRENT page (`container`)
and how to pull each named field out of one item (`fields`), returns one
record per item found. Deterministic and dumb on purpose — it does not infer
a container or guess field names; `read`'s surface (headings, landmarks,
controls) is what an agent uses to WRITE the spec in the first place.

New-in-web-drive (spec §4.3): browser.py is not touched, same discipline as
`read.py` and `probe.py`.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .browser import BrowserController

# `container`/each field-spec matcher, in the priority the spec examples use:
# an explicit CSS `selector` wins when given; otherwise an ARIA `role`,
# resolved against an explicit role="" attribute first and a small
# implicit-role table (spec examples name "listitem" and "heading") second.
_EXTRACT_JS = r"""
(spec) => {
  const IMPLICIT_ROLE = {
    LI: 'listitem', ARTICLE: 'article', TR: 'row', TABLE: 'table',
    UL: 'list', OL: 'list', A: 'link', BUTTON: 'button', IMG: 'img',
    H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading',
    H5: 'heading', H6: 'heading',
  };

  const byRole = (root, role) => {
    const explicit = root.querySelectorAll('[role="' + role + '"]');
    if (explicit.length) return Array.from(explicit);
    const tags = Object.keys(IMPLICIT_ROLE).filter((t) => IMPLICIT_ROLE[t] === role);
    if (!tags.length) return [];
    return Array.from(root.querySelectorAll(tags.join(',')));
  };

  const matchContainers = (containerSpec) => {
    if (containerSpec.selector) {
      return Array.from(document.querySelectorAll(containerSpec.selector));
    }
    if (containerSpec.role) {
      return byRole(document, containerSpec.role);
    }
    return [];
  };

  const resolveTarget = (container, fieldSpec) => {
    if (fieldSpec.selector) return container.querySelector(fieldSpec.selector);
    if (fieldSpec.role) {
      const found = byRole(container, fieldSpec.role);
      return found.length ? found[0] : null;
    }
    return container; // no selector/role -- the container element itself
  };

  const extractField = (container, fieldSpec) => {
    const target = resolveTarget(container, fieldSpec);
    if (!target) return null;
    if (fieldSpec.attr) return target.getAttribute(fieldSpec.attr);
    return (target.innerText || target.textContent || '').trim();
  };

  const containers = matchContainers(spec.container || {});
  return containers.map((c) => {
    const record = {};
    const fields = spec.fields || {};
    for (const name of Object.keys(fields)) {
      record[name] = extractField(c, fields[name]);
    }
    return record;
  });
}
"""


async def extract_records(
    controller: BrowserController,
    container: Dict[str, Any],
    fields: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Evaluate `_EXTRACT_JS` against the CURRENT page and return one record
    per matched container element.

    The caller navigates first (same convention as `read`/`probe`): extraction
    operates on one page the agent already chose, not a crawl.
    """
    raw: List[Dict[str, Any]] = await controller.page.evaluate(
        _EXTRACT_JS, {"container": container, "fields": fields}
    )
    return list(raw)
