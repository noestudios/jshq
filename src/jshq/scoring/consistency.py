"""Sibling-consistency check on categorical reads (2026-08-08, improvement plan).

`leads_discipline` and `management_type` are MODEL reads, and they derive
function_check_flag and the score caps — so one divergent read across a set of
near-identical postings puts one sibling on the other side of a 35-point
ceiling. The motivating case: two postings read `unclear` where four
near-identical siblings read `engineering`, so they sat uncapped at 27 while
the siblings sat on the wrong_function cap at 20.

Siblings are defined by TEXT, not title: two postings at one company are
siblings iff the Jaccard overlap of their normalized JD blocks — the same
blank-line blocks and _key normalization boilerplate.py fingerprints — is at
least SIBLING_JACCARD. Measured on the live board 2026-08-08: true sibling
pairs land at 0.61-1.00 (block floor 100), unrelated same-company pairs at
<= 0.20 with two same-family director pairs at 0.55-0.59 that agree on every
read anyway, and the six per-team Apple Product Designer postings — similar
roles, genuinely DIFFERENT text — pair at 0.00 and correctly never cluster.
0.5 splits the gap. Components are single-linkage; a company posts at most a
few dozen active roles, so the O(n^2) pass is nothing.

Each cluster puts each field to a strict-majority vote, with two asymmetries:

- `unclear` never overrides a definite read: it is absence of evidence, not
  evidence (the same null-vs-0 principle the silence values encode). A
  definite majority overriding an `unclear` minority IS the fix.
- an IC-designated row's management_type is never corrected: the categorical
  override in derive() owns that field whatever the model read, so a
  "correction" would only falsify the recorded model read.

Only rows scored in the CURRENT run are ever corrected — stored siblings
contribute votes but are never rewritten. A full rescore therefore self-heals
every cluster, and an incremental refresh checks each new posting against the
stored majority. management_type votes use the MODEL's read
(model_management_type where the IC override fired): that is the read whose
consistency is in question.

Pure decision logic, no writes (stored_members does SELECTs only):
run_scoring applies corrections through _write; scripts/score_distribution.py
applies the same corrections to its in-memory predictions — one
implementation, so the dry run keeps mirroring the write path.
"""

import itertools
import json
from collections import Counter, defaultdict

from . import boilerplate

SIBLING_JACCARD = 0.5
FIELDS = ("leads_discipline", "management_type")


def block_keys(text: str | None) -> set[str]:
    """Normalized fingerprints of a JD's substantial blocks — boilerplate.py's
    exact notion (>= MIN_BLOCK_CHARS after normalization), reused so "block"
    means one thing across the package."""
    return {
        k
        for k in (boilerplate._key(b) for b in boilerplate._blocks(text or ""))
        if len(k) >= boilerplate.MIN_BLOCK_CHARS
    }


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _components(members: list[dict]) -> list[list[int]]:
    """Single-linkage components (indexes into members) over sibling edges."""
    parent = list(range(len(members)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    keysets = [block_keys(m["text"]) for m in members]
    for i, j in itertools.combinations(range(len(members)), 2):
        if _jaccard(keysets[i], keysets[j]) >= SIBLING_JACCARD:
            parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(len(members)):
        groups[find(i)].append(i)
    return [g for g in groups.values() if len(g) >= 2]


def corrections(members: list[dict]) -> list[dict]:
    """Strict-minority reads among FRESH members, per cluster, per field.

    Each member: {id, company_id, text, reads: {field: value},
    ic_designated: bool, fresh: bool}. Returns
    [{id, field, from, to, agree, size}] — `agree` of `size` cluster members
    carry the majority value. Grouped by company first: boilerplate is mostly
    company-specific, but two employers sharing an agency template must never
    vote on each other's reads.
    """
    out = []
    by_company = defaultdict(list)
    for m in members:
        by_company[m["company_id"]].append(m)
    for company_members in by_company.values():
        for group in _components(company_members):
            for field in FIELDS:
                votes = Counter(company_members[i]["reads"][field] for i in group)
                value, n = votes.most_common(1)[0]
                # A strict majority (> half) is unique by definition; `unclear`
                # winning one is still absence, so it corrects nothing.
                if n * 2 <= len(group) or value == "unclear":
                    continue
                for i in group:
                    m = company_members[i]
                    if not m["fresh"] or m["reads"][field] == value:
                        continue
                    if field == "management_type" and m["ic_designated"]:
                        continue
                    out.append({
                        "id": m["id"], "field": field,
                        "from": m["reads"][field], "to": value,
                        "agree": n, "size": len(group),
                    })
    return out


def stored_members(conn, company_ids, exclude_ids) -> list[dict]:
    """Member records for the stored active scored siblings of a run — voters
    only (fresh=False), never rewritten. Old-rubric rows without both reads are
    skipped rather than guessed at."""
    # Lazy: this package's __init__ imports this module (same cycle-avoidance
    # as learned.py).
    from . import is_ic_designated

    company_ids, exclude_ids = sorted(company_ids), sorted(exclude_ids)
    if not company_ids:
        return []
    # NOT IN () has no SQL spelling — an empty exclusion means no clause at
    # all (NOT IN (NULL) would silently exclude every row).
    exclude_sql = (
        f" AND id NOT IN ({', '.join('?' * len(exclude_ids))})" if exclude_ids else ""
    )
    rows = conn.execute(
        f"""SELECT id, company_id, title, level_band, description_text, score_detail
            FROM jobs
            WHERE company_id IN ({", ".join("?" * len(company_ids))})
              AND status = 'active' AND score_detail IS NOT NULL
              AND description_text IS NOT NULL{exclude_sql}""",
        (*company_ids, *exclude_ids),
    ).fetchall()
    members = []
    for row in rows:
        detail = json.loads(row["score_detail"])
        leads, mgmt = detail.get("leads_discipline"), detail.get("management_type")
        if not leads or not mgmt:
            continue
        members.append({
            "id": row["id"], "company_id": row["company_id"],
            "text": row["description_text"],
            "reads": {
                "leads_discipline": leads,
                "management_type": detail.get("model_management_type") or mgmt,
            },
            "ic_designated": is_ic_designated(row["title"], row["level_band"]),
            "fresh": False,
        })
    return members
