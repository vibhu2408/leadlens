"""Deduplication.

Two rows are the same lead when they share an email, or when they describe the
same contact at the same company. "Same company" means the same domain or, when
no domain is known, a fuzzy match on the cleaned company name in the same state
("Acme Plumbing LLC" vs "ACME Plumbing, Inc."). Duplicates are merged rather
than dropped: blank fields are filled from the other rows.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

SUFFIXES = r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|co|corp|corporation|company|group|pllc|pc|lp|llp)\b\.?"
FUZZY_THRESHOLD = 92


def company_key(name: str) -> str:
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(SUFFIXES, " ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _person_key(rec: dict) -> str:
    return re.sub(r"[^a-z]", "", (rec.get("contact_name") or "").lower())


def _filled(rec: dict) -> int:
    return sum(1 for v in rec.values() if v not in (None, "", []))


def _merge(primary: dict, other: dict) -> None:
    for k, v in other.items():
        if primary.get(k) in (None, "") and v not in (None, ""):
            primary[k] = v


def dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    """Return ([{data, domain, dup_count}], duplicates_removed)."""
    groups: list[list[dict]] = []
    group_domain: list[str] = []  # first known domain of each group
    by_email: dict[str, int] = {}
    by_domain_person: dict[tuple[str, str], int] = {}
    # fuzzy candidates bucketed by (state, person) so matching stays ~linear on big files
    seen: dict[tuple[str, str], list[tuple[str, int]]] = {}  # -> [(company_key, group_idx)]

    for rec in rows:
        email = rec.get("email") or ""
        dom = rec.get("domain") or ""
        person = _person_key(rec)
        ckey = company_key(rec.get("company", ""))
        state = (rec.get("state") or "").lower()

        idx = by_email.get(email) if email else None
        if idx is None and dom:
            idx = by_domain_person.get((dom, person))
        if idx is None and ckey:
            # Fuzzy company match, only when the domains can't contradict each other.
            for k, gi in seen.get((state, person), []):
                d2 = group_domain[gi]
                if (not dom or not d2 or dom == d2) and fuzz.token_sort_ratio(k, ckey) >= FUZZY_THRESHOLD:
                    idx = gi
                    break

        if idx is None:
            idx = len(groups)
            groups.append([])
            group_domain.append("")
        groups[idx].append(rec)
        if dom and not group_domain[idx]:
            group_domain[idx] = dom
        if ckey:
            seen.setdefault((state, person), []).append((ckey, idx))
        if email:
            by_email.setdefault(email, idx)
        if dom:
            by_domain_person.setdefault((dom, person), idx)

    out = []
    for g in groups:
        g.sort(key=_filled, reverse=True)
        primary = dict(g[0])
        for other in g[1:]:
            _merge(primary, other)
        out.append({"data": primary, "domain": primary.get("domain"), "dup_count": len(g) - 1})
    return out, len(rows) - len(out)
