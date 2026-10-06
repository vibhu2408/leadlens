"""Explainable ICP scoring.

Every lead gets a 0-100 score built from weighted factors, and every factor
returns a human-readable reason, so a rep can see *why* a lead is an A, not just
that it is one. Two presets ship:

* acquisition: Caprae's own use case (ETA / PE deal sourcing). Rewards established,
  owner-operated, lower-middle-market businesses with succession signals.
* sales: classic B2B outbound. Rewards decision-maker contacts at growing,
  digitally active companies with deliverable inboxes.

Weights and ranges are user-editable; re-scoring is instant because enrichment
is stored on each lead.
"""
from __future__ import annotations

import copy
import re
from datetime import date

PRESETS: dict[str, dict] = {
    "acquisition": {
        "label": "Acquisition target (ETA / PE)",
        "industries": ["hvac", "plumbing", "electrical", "roofing", "landscaping", "manufacturing", "distribution",
                       "logistics", "trucking", "accounting", "insurance", "staffing", "it services", "msp",
                       "pest control", "dental", "veterinary", "machine shop", "fabrication", "janitorial", "construction"],
        "exclude_keywords": ["franchise", "non-profit", "nonprofit", "government", "university", "venture-backed"],
        "employees": [10, 150],
        "revenue": [2_000_000, 25_000_000],
        "min_age_years": 15,
        "titles": ["owner", "founder", "president", "ceo", "principal", "partner", "proprietor"],
        "weights": {"firmographic_fit": 25, "industry_fit": 15, "succession_signal": 20,
                    "decision_maker": 15, "reachability": 15, "data_quality": 10},
    },
    "sales": {
        "label": "B2B sales outreach",
        "industries": ["software", "saas", "it services", "marketing", "e-commerce", "retail", "manufacturing",
                       "healthcare", "financial services", "real estate", "logistics"],
        "exclude_keywords": ["government", "non-profit", "nonprofit"],
        "employees": [20, 500],
        "revenue": [2_000_000, 100_000_000],
        "min_age_years": 0,
        "titles": ["ceo", "founder", "owner", "president", "vp", "vice president", "head of", "director", "chief", "cmo", "cro", "coo", "cto"],
        "weights": {"firmographic_fit": 20, "industry_fit": 15, "growth_signal": 20,
                    "decision_maker": 20, "reachability": 15, "data_quality": 10},
    },
}


def default_icp(preset: str) -> dict:
    return copy.deepcopy(PRESETS.get(preset, PRESETS["acquisition"]))


def _range_fit(value: float | None, lo: float, hi: float) -> float:
    """1.0 inside [lo, hi], decaying to 0 at half / double the bounds."""
    if value is None:
        return 0.35  # unknown, neither rewarded nor fully punished
    if lo <= value <= hi:
        return 1.0
    if value < lo:
        return max(0.0, (value - lo / 2) / (lo / 2)) if lo else 0.0
    return max(0.0, 1 - (value - hi) / hi) if hi else 0.0


def _fmt_money(v: float) -> str:
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def _factor_firmographic(d, e, icp):
    emp_lo, emp_hi = icp["employees"]
    rev_lo, rev_hi = icp["revenue"]
    fe = _range_fit(d.get("employees"), emp_lo, emp_hi)
    fr = _range_fit(d.get("revenue"), rev_lo, rev_hi)
    bits = []
    bits.append(f"{int(d['employees'])} employees" if d.get("employees") else "employees unknown")
    bits.append(_fmt_money(d["revenue"]) + " revenue" if d.get("revenue") else "revenue unknown")
    return (fe + fr) / 2, f"{', '.join(bits)} vs target {emp_lo}-{emp_hi} staff, {_fmt_money(rev_lo)}-{_fmt_money(rev_hi)}"


def _haystack(d, e) -> str:
    return " ".join([d.get("industry") or "", d.get("company") or "", e.get("title") or "", e.get("description") or ""]).lower()


def _factor_industry(d, e, icp):
    hay = _haystack(d, e)
    for kw in icp.get("exclude_keywords", []):
        if kw and kw.lower() in hay:
            return 0.0, f"excluded: matches '{kw}'"
    hits = [i for i in icp["industries"] if i and re.search(r"\b" + re.escape(i.lower()) + r"\b", hay)]
    if hits:
        return 1.0, f"target industry ({', '.join(hits[:2])})"
    if not d.get("industry") and not e.get("description"):
        return 0.35, "industry unknown"
    return 0.15, f"outside target industries ({d.get('industry') or 'from website copy'})"


def _factor_succession(d, e, icp):
    """Owner-transition likelihood: business age, stale web presence, owner-held contact."""
    score, why = 0.0, []
    founded = d.get("founded")
    min_age = icp.get("min_age_years", 15)
    if founded:
        age = date.today().year - founded
        if age >= min_age:
            # 0.3 at the minimum age, rising to 0.55 at 40+ years (founder likely near retirement)
            score += 0.3 + 0.25 * min(1.0, (age - min_age) / max(1, 40 - min_age))
            why.append(f"{age} yrs in business")
        else:
            why.append(f"only {age} yrs old")
    else:
        score += 0.15; why.append("founding year unknown")
    stale = e.get("site_age_years")
    if stale is not None and stale >= 3:
        score += 0.25; why.append(f"website untouched since {e['copyright_year']}")
    elif e.get("status") == "ok" and not e.get("tech"):
        score += 0.1; why.append("minimal web tooling")
    if any(t in (d.get("title") or "").lower() for t in ("owner", "founder", "proprietor")):
        score += 0.2; why.append("owner-operated")
    return min(score, 1.0), "; ".join(why)


def _factor_growth(d, e, icp):
    score, why = 0.0, []
    if e.get("hiring"):
        score += 0.4; why.append("actively hiring")
    tech = e.get("tech") or []
    marketing = [t for t in tech if t in ("HubSpot", "Salesforce", "Google Analytics", "Meta Pixel", "Intercom", "Drift")]
    if marketing:
        score += min(0.35, 0.15 * len(marketing)); why.append("invests in marketing tech (" + ", ".join(marketing[:3]) + ")")
    if e.get("site_age_years") is not None and e["site_age_years"] <= 1:
        score += 0.15; why.append("site recently updated")
    if e.get("socials", {}).get("linkedin"):
        score += 0.1; why.append("LinkedIn presence")
    if not why:
        return (0.3 if e.get("status") != "ok" else 0.1), ("no website data" if e.get("status") != "ok" else "no growth signals found")
    return min(score, 1.0), "; ".join(why)


def _factor_decision_maker(d, e, icp):
    title = (d.get("title") or "").lower()
    if not title:
        return (0.3, "contact named, title unknown") if d.get("contact_name") else (0.0, "no named contact")
    if any(re.search(r"\b" + re.escape(t) + r"\b", title) for t in icp["titles"]):
        return 1.0, f"decision maker ({d['title']})"
    if re.search(r"\b(manager|lead|senior)\b", title):
        return 0.5, f"influencer, not final say ({d['title']})"
    return 0.2, f"unlikely buyer ({d['title']})"


def _factor_reachability(d, e, icp, email_status, flags):
    s = {"valid": 0.75, "unverified": 0.5, "risky": 0.35, "missing": 0.1, "invalid": 0.0}[email_status]
    why = [f"email {email_status}" + (f" ({', '.join(flags)})" if flags else "")]
    if "role inbox" in flags:
        s -= 0.2  # info@/sales@ rarely reach the decision maker
    if "personal" in flags or "domain mismatch" in flags:
        s -= 0.1
    if d.get("phone") or e.get("phones"):
        s += 0.15; why.append("phone available")
    if d.get("linkedin") or e.get("socials", {}).get("linkedin"):
        s += 0.1; why.append("LinkedIn available")
    return max(0.0, min(s, 1.0)), "; ".join(why)


def _factor_quality(d, e, icp, dup_count):
    keys = ("company", "domain", "industry", "employees", "revenue", "city", "state", "contact_name", "title", "email", "phone")
    filled = sum(1 for k in keys if d.get(k))
    s = filled / len(keys)
    why = [f"{filled}/{len(keys)} key fields present"]
    if e.get("status") == "ok":
        s = min(1.0, s + 0.1); why.append("website verified live")
    elif e.get("status") in ("unreachable", "error"):
        s *= 0.6; why.append("website unreachable")
    if dup_count:
        why.append(f"merged {dup_count} duplicate{'s' if dup_count > 1 else ''}")
    return s, "; ".join(why)


LABELS = {
    "firmographic_fit": "Firmographic fit", "industry_fit": "Industry fit", "succession_signal": "Succession signal",
    "growth_signal": "Growth signal", "decision_maker": "Decision maker", "reachability": "Reachability",
    "data_quality": "Data quality",
}


def score_lead(data: dict, enrichment: dict, email_status: str, email_flags: list[str], dup_count: int, icp: dict):
    factors = {
        "firmographic_fit": lambda: _factor_firmographic(data, enrichment, icp),
        "industry_fit": lambda: _factor_industry(data, enrichment, icp),
        "succession_signal": lambda: _factor_succession(data, enrichment, icp),
        "growth_signal": lambda: _factor_growth(data, enrichment, icp),
        "decision_maker": lambda: _factor_decision_maker(data, enrichment, icp),
        "reachability": lambda: _factor_reachability(data, enrichment, icp, email_status, email_flags),
        "data_quality": lambda: _factor_quality(data, enrichment, icp, dup_count),
    }
    weights = {k: v for k, v in icp["weights"].items() if v > 0 and k in factors}
    total_w = sum(weights.values()) or 1
    reasons, total = [], 0.0
    for key, w in weights.items():
        val, why = factors[key]()
        pts = val * w / total_w * 100
        total += pts
        reasons.append({"key": key, "label": LABELS[key], "value": round(val, 2), "points": round(pts, 1),
                        "max": round(w / total_w * 100, 1), "why": why})
    score = int(round(total))
    # Hard gate: an excluded industry is never an A or B, whatever else it scores.
    if any(r["key"] == "industry_fit" and r["why"].startswith("excluded") for r in reasons):
        score = min(score, 34)
    # Can't email them: still workable by phone, but not a top-of-queue lead.
    if email_status in ("invalid", "missing"):
        score = min(score, 79)
    tier = "A" if score >= 82 else "B" if score >= 65 else "C" if score >= 45 else "D"
    return score, tier, reasons
