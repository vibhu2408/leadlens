import os
import tempfile

os.environ.setdefault("LEADLENS_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from app import scoring  # noqa: E402
from app.dedupe import company_key, dedupe  # noqa: E402
from app.enrich import _looks_blocked, parse_html  # noqa: E402
from app.ingest import map_headers, normalize_domain, parse_csv, parse_number  # noqa: E402
from app.validate import classify  # noqa: E402


# ---------- ingest ----------

def test_header_aliases():
    m = map_headers(["Company Name", "Website URL", "Owner Name", "E-mail", "Employee Count", "Year Founded"])
    assert m == {"company": "Company Name", "website": "Website URL", "contact_name": "Owner Name",
                 "email": "E-mail", "employees": "Employee Count", "founded": "Year Founded"}


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.com/about?x=1") == "acme.com"
    assert normalize_domain("acme.com") == "acme.com"
    assert normalize_domain("bob@acme.com") == "acme.com"
    assert normalize_domain("not a site") is None
    assert normalize_domain("") is None


def test_parse_number():
    assert parse_number("11-50") == 30.5
    assert parse_number("$1M - $5M") == 3_000_000
    assert parse_number("$1-5M") == 3_000_000
    assert parse_number("2.5M") == 2_500_000
    assert parse_number("$2,800,000") == 2_800_000
    assert parse_number("500+") == 500
    assert parse_number("$400K") == 400_000
    assert parse_number("") is None


def test_parse_csv_semicolon_and_first_last():
    csv = b"Business;First Name;Last Name;Email\nAcme;jane;DOE;Jane@Acme.com\n;;;\n"
    rows, _ = parse_csv(csv)
    assert len(rows) == 1
    assert rows[0]["contact_name"] == "Jane Doe"
    assert rows[0]["email"] == "jane@acme.com"
    assert rows[0]["domain"] == "acme.com"  # inferred from business email


# ---------- dedupe ----------

def test_company_key_strips_suffixes():
    assert company_key("ACME Plumbing, Inc.") == company_key("Acme Plumbing LLC") == "acme plumbing"


def test_dedupe_merges_and_keeps_distinct_contacts():
    rows = [
        {"company": "Acme Plumbing LLC", "domain": "acme.com", "email": "bob@acme.com", "contact_name": "Bob", "state": "CO", "phone": ""},
        {"company": "Acme Plumbing", "domain": "acme.com", "email": "bob@acme.com", "contact_name": "Bob", "state": "CO", "phone": "555"},
        {"company": "ACME PLUMBING INC", "domain": None, "email": "", "contact_name": "Bob", "state": "CO"},
        {"company": "Acme Plumbing", "domain": "acme.com", "email": "sue@acme.com", "contact_name": "Sue", "state": "CO"},
        {"company": "Acme Plumbing", "domain": "other.com", "email": "", "contact_name": "Bob", "state": "CO"},
    ]
    out, removed = dedupe(rows)
    assert removed == 2
    bob = next(o for o in out if o["data"]["email"] == "bob@acme.com")
    assert bob["dup_count"] == 2
    assert bob["data"]["phone"] == "555"  # gap filled from the duplicate
    assert len(out) == 3  # Sue is a separate contact; other.com is a different company


# ---------- validation ----------

def test_email_classify():
    assert classify("", "acme.com") == ("missing", [])
    assert classify("bob@acme", "acme.com")[0] == "invalid"
    assert classify("x@mailinator.com", None) == ("invalid", ["disposable"])
    assert classify("info@acme.com", "acme.com") == ("unverified", ["role inbox"])
    assert classify("bob@gmail.com", "acme.com") == ("unverified", ["personal"])
    assert classify("bob@acme.co", "acme.com") == ("unverified", ["domain mismatch"])
    assert classify("bob@mail.acme.com", "acme.com") == ("unverified", [])


# ---------- enrichment parsing ----------

HTML = """<html><head><title>Harbor HVAC | Tampa Heating & Cooling</title>
<meta name="description" content="Family owned since 1988.">
<script src="https://js.hs-scripts.com/123.js"></script></head>
<body><a href="mailto:owner@harbor.com">Email</a> <a href="tel:+18135550142">Call</a>
<a href="https://www.linkedin.com/company/harbor-hvac">in</a> <a href="/careers">Careers</a>
<a href="/contact-us">Contact</a> <img src="logo@2x.png"> office@harbor.com
<footer>&copy; 2015-2019 Harbor HVAC</footer></body></html>"""


def test_parse_html():
    d = parse_html(HTML, "harbor.com")
    assert d["title"].startswith("Harbor HVAC")
    assert d["description"] == "Family owned since 1988."
    assert d["emails"] == ["office@harbor.com", "owner@harbor.com"]
    assert d["phones"][0] == "+18135550142"
    assert "linkedin" in d["socials"]
    assert "HubSpot" in d["tech"]
    assert d["hiring"] is True
    assert d["copyright_year"] == 2019
    assert d["_contact_link"] == "/contact-us"


def test_captcha_detection():
    assert _looks_blocked(403, "<title>Attention Required! | Cloudflare</title>")
    assert _looks_blocked(200, "<html><div class='g-recaptcha'></div></html>")
    assert not _looks_blocked(200, "<html>" + "real content " * 1000 + "</html>")


# ---------- scoring ----------

GOOD = {"company": "Harbor HVAC", "domain": "harbor.com", "industry": "HVAC", "employees": 45, "revenue": 6e6,
        "founded": 1988, "city": "Tampa", "state": "FL", "contact_name": "Rob K", "title": "Owner",
        "email": "rob@harbor.com", "phone": "1"}


def test_scoring_ranks_fit_above_misfit():
    icp = scoring.default_icp("acquisition")
    good, tier, reasons = scoring.score_lead(GOOD, {"status": "ok", "copyright_year": 2018, "site_age_years": 8}, "valid", [], 0, icp)
    bad_data = {**GOOD, "industry": "Software", "employees": 900, "revenue": 3e8, "founded": 2021, "title": "Account Executive"}
    bad, _, _ = scoring.score_lead(bad_data, {"status": "ok"}, "valid", [], 0, icp)
    assert good > 80 and tier == "A"
    assert bad < 45
    assert abs(sum(r["max"] for r in reasons) - 100) < 0.5
    assert all(r["why"] for r in reasons)


def test_excluded_industry_is_capped():
    icp = scoring.default_icp("acquisition")
    score, tier, _ = scoring.score_lead({**GOOD, "industry": "Restaurant Franchise"}, {}, "valid", [], 0, icp)
    assert score <= 34 and tier == "D"


def test_presets_change_ranking():
    saas = {**GOOD, "industry": "SaaS", "founded": 2020, "title": "VP Sales", "employees": 80, "revenue": 1e7}
    e = {"status": "ok", "hiring": True, "tech": ["HubSpot", "Google Analytics"], "site_age_years": 0, "socials": {"linkedin": "x"}}
    acq = scoring.default_icp("acquisition")
    sales = scoring.default_icp("sales")
    assert scoring.score_lead(saas, e, "valid", [], 0, sales)[0] > scoring.score_lead(saas, e, "valid", [], 0, acq)[0]
