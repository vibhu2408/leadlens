"""CSV ingestion: map messy export headers onto one canonical lead schema.

Lead exports (SaaSquatch, Apollo, ZoomInfo, hand-built sheets) all name columns
differently. Rather than force users to rename columns, we match headers against
a list of aliases and normalise values (domains, numbers, names) on the way in.
"""
from __future__ import annotations

import csv
import io
import re
from urllib.parse import urlparse

CANONICAL_FIELDS = [
    "company", "website", "industry", "employees", "revenue", "founded",
    "city", "state", "country", "contact_name", "title", "email", "phone", "linkedin",
]

# Header aliases (lower-cased, punctuation stripped). First match wins.
ALIASES: dict[str, list[str]] = {
    "company": ["company", "company name", "companyname", "business", "business name", "organization", "account", "account name", "name"],
    "website": ["website", "website url", "domain", "url", "company website", "company domain", "site", "web"],
    "industry": ["industry", "sector", "category", "vertical", "naics description", "sic description"],
    "employees": ["employees", "employee count", "employee range", "number of employees", "headcount", "company size", "size", "staff"],
    "revenue": ["revenue", "annual revenue", "revenue range", "estimated revenue", "est revenue", "sales volume"],
    "founded": ["founded", "year founded", "founded year", "founding year", "established", "year established", "year started"],
    "city": ["city", "town", "locality"],
    "state": ["state", "region", "state region", "province", "state province"],
    "country": ["country", "country code", "nation"],
    "contact_name": ["contact name", "contact", "full name", "owner", "owner name", "person name", "decision maker", "lead name"],
    "first_name": ["first name", "firstname", "first", "given name"],
    "last_name": ["last name", "lastname", "last", "surname", "family name"],
    "title": ["title", "job title", "position", "role", "contact title", "designation"],
    "email": ["email", "email address", "contact email", "work email", "e mail", "mail", "owner email"],
    "phone": ["phone", "phone number", "telephone", "tel", "mobile", "contact phone", "company phone", "direct phone"],
    "linkedin": ["linkedin", "linkedin url", "linkedin profile", "person linkedin url", "li url"],
}


# Exports fill missing cells with these; treat them as empty.
PLACEHOLDERS = {"n/a", "na", "-", "--", "null", "none", "unknown", "not available", "#n/a"}


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def map_headers(headers: list[str]) -> dict[str, str]:
    """Return {canonical_field: original_header}."""
    normed = {_norm_header(h): h for h in headers}
    mapping: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        for a in aliases:
            if a in normed and normed[a] not in mapping.values():
                mapping[field] = normed[a]
                break
    return mapping


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if "@" in v and "/" not in v:  # someone put an email in the website column
        v = v.split("@", 1)[1]
    if not re.match(r"^[a-z]+://", v):
        v = "http://" + v
    host = urlparse(v).hostname or ""
    host = host.removeprefix("www.").strip(".")
    if "." not in host or " " in host:
        return None
    return host


def parse_number(value: str | None) -> float | None:
    """Parse '11-50', '$1M - $5M', '2.5M', '1,200', '500+' into a single representative number."""
    if value is None:
        return None
    s = str(value).strip().lower().replace(",", "").replace("$", "").replace("usd", "")
    if not s:
        return None
    mult = {"k": 1e3, "m": 1e6, "mm": 1e6, "b": 1e9, "bn": 1e9}
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(mm|bn|k|m|b)?\b", s)
    if not parts:
        return None
    if len(parts) >= 2:  # range -> midpoint
        (lo, lo_sfx), (hi, hi_sfx) = parts[0], parts[1]
        lo_sfx = lo_sfx or hi_sfx  # "$1-5M": unit written once applies to both ends
        return (float(lo) * mult.get(lo_sfx, 1) + float(hi) * mult.get(hi_sfx, 1)) / 2
    num, sfx = parts[0]
    return float(num) * mult.get(sfx, 1)


def parse_year(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"(18|19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


def clean_name(value: str | None) -> str:
    v = re.sub(r"\s+", " ", (value or "")).strip()
    # Fix SHOUTING or all-lowercase words; leave mixed-case ones (e.g. "McDonald") alone.
    return " ".join(w.capitalize() if (w.isupper() or w.islower()) and len(w) > 1 else w for w in v.split(" "))


def parse_csv(content: bytes) -> tuple[list[dict], dict[str, str]]:
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    mapping = map_headers(headers)
    rows: list[dict] = []
    for raw in reader:
        def get(f: str) -> str:
            v = (raw.get(mapping[f]) or "").strip() if f in mapping else ""
            return "" if v.lower() in PLACEHOLDERS else v
        contact = get("contact_name") or " ".join(p for p in (get("first_name"), get("last_name")) if p)
        if not any(get(f) for f in ("company", "website", "email")):
            continue  # blank / junk row
        rec = {
            "company": clean_name(get("company")) if get("company").isupper() else get("company"),
            "website": get("website"),
            "industry": get("industry"),
            "employees_raw": get("employees"),
            "employees": parse_number(get("employees")),
            "revenue_raw": get("revenue"),
            "revenue": parse_number(get("revenue")),
            "founded": parse_year(get("founded")),
            "city": get("city"),
            "state": get("state"),
            "country": get("country"),
            "contact_name": clean_name(contact),
            "title": get("title"),
            "email": get("email").lower(),
            "phone": get("phone"),
            "linkedin": get("linkedin"),
        }
        rec["domain"] = normalize_domain(rec["website"]) or _domain_from_business_email(rec["email"])
        rows.append(rec)
    return rows, mapping


FREE_MAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com",
    "msn.com", "comcast.net", "att.net", "verizon.net", "me.com", "protonmail.com", "proton.me", "ymail.com",
}


def _domain_from_business_email(email: str) -> str | None:
    if "@" not in email:
        return None
    d = email.rsplit("@", 1)[1].strip().lower()
    return None if d in FREE_MAIL else normalize_domain(d)
