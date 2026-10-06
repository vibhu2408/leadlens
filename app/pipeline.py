"""Batch pipeline: parse -> dedupe -> enrich -> verify -> score.

Runs as an in-process background task. For multi-instance deployments this
function is the unit of work you would hand to a queue (Cloud Tasks / SQS / RQ);
it only talks to the outside world through `db`.
"""
from __future__ import annotations

import time
import traceback

from . import db, enrich, scoring, validate
from .dedupe import dedupe


def _fill_from_site(data: dict, e: dict) -> list[str]:
    """Use what the website gave us to fill gaps in the record."""
    filled = []
    if not data.get("email") and e.get("emails"):
        # prefer a personal-looking on-domain address over info@ / sales@
        personal = [x for x in e["emails"] if x.split("@")[0] not in validate.ROLE_PREFIXES]
        data["email"] = (personal or e["emails"])[0]
        data["email_source"] = "website"
        filled.append("email")
    if not data.get("phone") and e.get("phones"):
        data["phone"] = e["phones"][0]
        filled.append("phone")
    if not data.get("linkedin") and e.get("socials", {}).get("linkedin"):
        data["linkedin"] = e["socials"]["linkedin"]
        filled.append("linkedin")
    return filled


def rescore(batch_id: str, icp: dict) -> dict:
    leads = db.get_leads(batch_id)
    rows, tiers = [], {"A": 0, "B": 0, "C": 0, "D": 0}
    for l in leads:
        score, tier, reasons = scoring.score_lead(l["data"], l["enrichment"], l["email_status"], l["email_flags"],
                                                  l["dup_count"], icp)
        tiers[tier] += 1
        rows.append((l["id"], {**l, "score": score, "tier": tier, "reasons": reasons}))
    db.update_leads_bulk(rows)
    return tiers


async def process_batch(batch_id: str, rows: list[dict], network: bool) -> None:
    t0 = time.time()
    try:
        batch = db.get_batch(batch_id)
        icp = batch["icp"]
        db.update_batch(batch_id, status="processing", stage="Deduplicating", total=len(rows))

        unique, removed = dedupe(rows)
        db.insert_leads(batch_id, unique)
        leads = db.get_leads(batch_id)

        # --- enrich (one fetch per unique domain, shared across leads) ---
        domains = sorted({l["domain"] for l in leads if l["domain"]})
        site: dict[str, dict] = {}
        cache_hits = 0
        if network and domains:
            db.update_batch(batch_id, stage="Enriching websites", total=len(domains), processed=0)
            done = {"n": 0, "t": 0.0}

            def tick():
                done["n"] += 1
                if time.time() - done["t"] > 0.4 or done["n"] == len(domains):
                    done["t"] = time.time()
                    db.update_batch(batch_id, processed=done["n"])

            site = await enrich.enrich_many(domains, on_progress=tick)
            cache_hits = sum(1 for v in site.values() if v.get("cached"))

        filled_counts = {"email": 0, "phone": 0, "linkedin": 0}
        for l in leads:
            l["enrichment"] = site.get(l["domain"], {"status": "skipped"} if not network else {"status": "no domain"})
            for f in _fill_from_site(l["data"], l["enrichment"]):
                filled_counts[f] += 1

        # --- verify emails ---
        db.update_batch(batch_id, stage="Verifying emails", total=len(leads), processed=0)
        checks = await validate.check_many([(l["data"].get("email", ""), l["domain"]) for l in leads], network)

        # --- score ---
        db.update_batch(batch_id, stage="Scoring")
        out, tiers = [], {"A": 0, "B": 0, "C": 0, "D": 0}
        email_counts: dict[str, int] = {}
        site_counts: dict[str, int] = {}
        for l, (status, flags) in zip(leads, checks):
            if l["data"].get("email_source") == "website":
                flags = flags + ["found on website"]
            score, tier, reasons = scoring.score_lead(l["data"], l["enrichment"], status, flags, l["dup_count"], icp)
            tiers[tier] += 1
            email_counts[status] = email_counts.get(status, 0) + 1
            s = l["enrichment"].get("status", "skipped")
            site_counts[s] = site_counts.get(s, 0) + 1
            out.append((l["id"], {"data": l["data"], "email_status": status, "email_flags": flags, "enrichment": l["enrichment"],
                                  "score": score, "tier": tier, "reasons": reasons}))
        db.update_leads_bulk(out)

        db.update_batch(
            batch_id, status="done", stage="Done", processed=len(leads),
            stats={
                "rows_imported": len(rows), "duplicates_removed": removed, "unique_leads": len(leads),
                "domains": len(domains), "cache_hits": cache_hits, "tiers": tiers, "email": email_counts,
                "sites": site_counts, "filled_from_site": filled_counts, "seconds": round(time.time() - t0, 1),
            },
        )
    except Exception as exc:  # surface failures in the UI instead of a stuck spinner
        traceback.print_exc()
        db.update_batch(batch_id, status="error", stage=f"Failed: {type(exc).__name__}: {exc}")
