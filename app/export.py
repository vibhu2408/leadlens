"""CSV exports: a HubSpot-ready import file and a full, analyst-friendly dump."""
from __future__ import annotations

import csv
import io

HUBSPOT_STATUS = {"new": "NEW", "contacted": "ATTEMPTED_TO_CONTACT", "qualified": "OPEN_DEAL", "disqualified": "UNQUALIFIED"}


def _split_name(name: str) -> tuple[str, str]:
    parts = (name or "").split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def _top_reasons(lead: dict, n: int = 3) -> str:
    rs = sorted(lead["reasons"], key=lambda r: r["value"], reverse=True)[:n]
    return " | ".join(f"{r['label']}: {r['why']}" for r in rs)


def to_hubspot(leads: list[dict]) -> str:
    """Column names match HubSpot's default contact+company import mapping."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["First Name", "Last Name", "Email", "Phone Number", "Job Title", "Company Name", "Website URL",
                "Industry", "City", "State/Region", "Number of Employees", "Annual Revenue", "Lead Status",
                "LeadLens Score", "LeadLens Tier", "LeadLens Notes"])
    for l in leads:
        d, e = l["data"], l["enrichment"]
        first, last = _split_name(d.get("contact_name", ""))
        phone = d.get("phone") or (e.get("phones") or [""])[0]
        w.writerow([
            first, last, d.get("email", "") if l["email_status"] != "invalid" else "", phone, d.get("title", ""),
            d.get("company", ""), d.get("domain") or "", d.get("industry", ""), d.get("city", ""), d.get("state", ""),
            int(d["employees"]) if d.get("employees") else "", int(d["revenue"]) if d.get("revenue") else "",
            HUBSPOT_STATUS.get(l["status"], "NEW"), l["score"], l["tier"], _top_reasons(l),
        ])
    return buf.getvalue()


def to_full_csv(leads: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["score", "tier", "status", "company", "domain", "industry", "employees", "revenue", "founded", "city",
                "state", "contact_name", "title", "email", "email_status", "email_flags", "phone", "linkedin",
                "site_status", "site_title", "tech", "hiring", "copyright_year", "site_emails", "duplicates_merged",
                "reasons", "opener"])
    for l in leads:
        d, e = l["data"], l["enrichment"]
        w.writerow([
            l["score"], l["tier"], l["status"], d.get("company"), d.get("domain"), d.get("industry"),
            d.get("employees_raw") or d.get("employees"), d.get("revenue_raw") or d.get("revenue"), d.get("founded"),
            d.get("city"), d.get("state"), d.get("contact_name"), d.get("title"), d.get("email"), l["email_status"],
            "; ".join(l["email_flags"]), d.get("phone") or "; ".join(e.get("phones") or []),
            d.get("linkedin") or (e.get("socials") or {}).get("linkedin", ""), e.get("status", "skipped"),
            e.get("title", ""), "; ".join(e.get("tech") or []), e.get("hiring", ""), e.get("copyright_year", ""),
            "; ".join(e.get("emails") or []), l["dup_count"], _top_reasons(l, 7), l.get("opener") or "",
        ])
    return buf.getvalue()
