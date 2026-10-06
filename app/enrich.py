"""Website enrichment: turn a bare domain into signals a sales or deal team can use.

How it fetches:
* async httpx, a global concurrency cap and one request at a time per host
* honours robots.txt, sends an honest User-Agent, caps body size, retries once with backoff
* detects CAPTCHA / bot-wall pages and reports them as "blocked" instead of parsing junk
* results are cached per domain in SQLite for 7 days, so re-imports and overlapping
  batches cost nothing
* outbound proxies (for IP-restricted targets) come from the standard HTTPS_PROXY env var

What it extracts: title and description, on-domain emails, phones, social profiles,
tech stack, copyright year (how fresh the site is), hiring signal, and e-commerce signal.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import db

USER_AGENT = "LeadLensBot/1.0 (+https://github.com/; lead research; respects robots.txt)"
TIMEOUT = httpx.Timeout(8.0, connect=4.0)
MAX_BYTES = 1_500_000
GLOBAL_CONCURRENCY = 12

TECH_SIGNATURES = {
    "Shopify": [r"cdn\.shopify\.com", r"shopify\.theme"],
    "WordPress": [r"wp-content/", r"wp-includes/"],
    "WooCommerce": [r"woocommerce"],
    "Wix": [r"static\.wixstatic\.com", r"wix\.com"],
    "Squarespace": [r"squarespace\.com", r"static1\.squarespace"],
    "Webflow": [r"webflow\.(com|io)", r"data-wf-page"],
    "HubSpot": [r"js\.hs-scripts\.com", r"hs-analytics", r"hsforms"],
    "Salesforce": [r"pardot", r"force\.com", r"salesforce"],
    "Google Analytics": [r"googletagmanager\.com", r"google-analytics\.com", r"gtag\("],
    "Meta Pixel": [r"connect\.facebook\.net", r"fbq\("],
    "Intercom": [r"widget\.intercom\.io"],
    "Drift": [r"js\.driftt\.com"],
    "Calendly": [r"calendly\.com"],
    "Stripe": [r"js\.stripe\.com"],
    "ServiceTitan": [r"servicetitan"],
    "Housecall Pro": [r"housecallpro"],
}
SOCIAL = {
    "linkedin": r"linkedin\.com/(company|in)/",
    "facebook": r"facebook\.com/",
    "x": r"(twitter|x)\.com/",
    "instagram": r"instagram\.com/",
    "youtube": r"youtube\.com/",
}
CAPTCHA_MARKERS = [
    "cf-chl", "challenge-platform", "attention required! | cloudflare", "g-recaptcha",
    "hcaptcha", "are you a robot", "verify you are human", "access denied",
]
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")
YEAR_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)

_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)


def parse_html(html: str, domain: str) -> dict:
    """Pure parsing step (unit-tested separately from the network)."""
    soup = BeautifulSoup(html, "html.parser")
    low = html.lower()
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = (desc_tag.get("content") or "").strip() if desc_tag else ""

    hrefs = [a.get("href") or "" for a in soup.find_all("a")]
    text = soup.get_text(" ", strip=True)

    emails = set()
    for h in hrefs:
        if h.lower().startswith("mailto:"):
            emails.add(h[7:].split("?")[0].strip().lower())
    for m in EMAIL_RE.findall(text):
        emails.add(m.lower())
    # keep on-domain addresses only; drop image names like logo@2x.png
    emails = sorted(e for e in emails if e.endswith("@" + domain) or e.endswith("." + domain))

    phones = []
    for h in hrefs:
        if h.lower().startswith("tel:"):
            phones.append(h[4:].strip())
    phones += PHONE_RE.findall(text)
    phones = list(dict.fromkeys(p.strip() for p in phones))[:3]

    socials = {}
    for name, pat in SOCIAL.items():
        for h in hrefs:
            if re.search(pat, h, re.I) and "share" not in h.lower():
                socials[name] = h
                break

    tech = [t for t, pats in TECH_SIGNATURES.items() if any(re.search(p, low) for p in pats)]

    years = [int(y) for y in YEAR_RE.findall(html)]
    copyright_year = max(years) if years else None

    hiring = any(re.search(r"/(careers?|jobs|join-us|work-with-us|employment)\b", h, re.I) for h in hrefs) or bool(
        re.search(r"\b(we'?re hiring|now hiring|join our team)\b", text, re.I)
    )
    ecommerce = any(t in tech for t in ("Shopify", "WooCommerce")) or bool(re.search(r"add to cart|/cart\b|checkout", low))

    contact_link = next(
        (h for h in hrefs if re.search(r"/(contact|contact-us|about|about-us)/?$", h, re.I)), None
    )
    return {
        "title": title[:200],
        "description": description[:400],
        "emails": emails[:5],
        "phones": phones,
        "socials": socials,
        "tech": tech,
        "copyright_year": copyright_year,
        "site_age_years": (date.today().year - copyright_year) if copyright_year else None,
        "hiring": hiring,
        "ecommerce": ecommerce,
        "word_count": len(text.split()),
        "_contact_link": contact_link,
    }


def _looks_blocked(status: int, html: str) -> bool:
    low = html[:20000].lower()
    return status in (403, 429, 503) and any(m in low for m in CAPTCHA_MARKERS) or (
        len(low) < 5000 and any(m in low for m in CAPTCHA_MARKERS[:6])
    )


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    for attempt in range(2):
        try:
            async with client.stream("GET", url) as r:
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                r._content = b"".join(chunks)  # noqa: SLF001 - keep partial body
                if r.status_code in (429, 502, 503, 504) and attempt == 0:
                    await asyncio.sleep(1.5)
                    continue
                return r
        except (httpx.TransportError, httpx.TooManyRedirects):
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            return None
    return None


async def _robots_allows(client: httpx.AsyncClient, base: str) -> bool:
    try:
        r = await client.get(urljoin(base, "/robots.txt"), timeout=4.0)
    except httpx.HTTPError:
        return True
    if r.status_code >= 400:
        return True
    rp = robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(USER_AGENT, base + "/")


async def _fetch_domain(client: httpx.AsyncClient, domain: str) -> dict:
    started = time.time()
    for scheme in ("https", "http"):
        base = f"{scheme}://{domain}"
        if not await _robots_allows(client, base):
            return {"status": "disallowed", "note": "robots.txt disallows crawling"}
        r = await _get(client, base + "/")
        if r is None:
            continue
        html = r.text
        if _looks_blocked(r.status_code, html):
            return {"status": "blocked", "note": f"bot protection / CAPTCHA (HTTP {r.status_code})"}
        if r.status_code >= 400:
            return {"status": "error", "note": f"HTTP {r.status_code}"}
        data = parse_html(html, domain)
        final_host = (urlparse(str(r.url)).hostname or "").removeprefix("www.")
        # One extra hop: contact/about pages are where SMBs put emails & phones.
        link = data.pop("_contact_link")
        if link and (not data["emails"] or not data["phones"]):
            r2 = await _get(client, urljoin(str(r.url), link))
            if r2 is not None and r2.status_code < 400:
                extra = parse_html(r2.text, domain)
                data["emails"] = sorted(set(data["emails"]) | set(extra["emails"]))[:5]
                data["phones"] = list(dict.fromkeys(data["phones"] + extra["phones"]))[:3]
                for k, v in extra["socials"].items():
                    data["socials"].setdefault(k, v)
        data.update(
            status="ok",
            https=str(r.url).startswith("https://"),
            redirected_to=final_host if final_host and final_host != domain else None,
            fetch_ms=int((time.time() - started) * 1000),
        )
        return data
    return {"status": "unreachable", "note": "no response over https or http"}


async def enrich_domain(client: httpx.AsyncClient, domain: str) -> dict:
    cached = db.cache_get_enrichment(domain)
    if cached is not None:
        return {**cached, "cached": True}
    async with _sem:
        data = await _fetch_domain(client, domain)
    if data["status"] in ("ok", "disallowed", "blocked"):  # don't cache transient failures
        db.cache_put_enrichment(domain, data)
    return data


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
        limits=httpx.Limits(max_connections=GLOBAL_CONCURRENCY * 2, max_keepalive_connections=GLOBAL_CONCURRENCY),
        trust_env=True,  # honours HTTPS_PROXY for IP-restricted targets
    )


async def enrich_many(domains: list[str], on_progress=None) -> dict[str, dict]:
    results: dict[str, dict] = {}
    async with make_client() as client:
        async def one(d: str):
            try:
                results[d] = await enrich_domain(client, d)
            except Exception as e:  # never let one site kill the batch
                results[d] = {"status": "error", "note": type(e).__name__}
            if on_progress:
                on_progress()

        await asyncio.gather(*(one(d) for d in domains))
    return results
