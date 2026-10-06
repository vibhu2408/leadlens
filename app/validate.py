"""Email validation: syntax, address type, and deliverability (MX record).

We stop at MX lookup on purpose. SMTP "ping" verification (RCPT TO probing)
gets sending IPs blacklisted and is unreliable against catch-all servers; a
dedicated verifier API (ZeroBounce, NeverBounce) is the right next step and
would plug into `check_email` without touching callers.
"""
from __future__ import annotations

import asyncio
import re

import dns.asyncresolver
import dns.exception
import dns.resolver

from . import db
from .ingest import FREE_MAIL

EMAIL_RE = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}$")
ROLE_PREFIXES = {
    "info", "sales", "admin", "contact", "support", "hello", "office", "team", "help", "service",
    "billing", "marketing", "noreply", "no-reply", "webmaster", "enquiries", "inquiries", "careers", "jobs", "hr",
    "customersupport", "customerservice", "customercare", "custserv", "orders", "reception", "frontdesk", "media", "press",
}
DISPOSABLE = {"mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com", "yopmail.com", "trashmail.com"}


def classify(email: str, company_domain: str | None) -> tuple[str, list[str]]:
    """Offline checks. Returns (status, flags); status is 'missing' | 'invalid' | 'unverified'."""
    if not email:
        return "missing", []
    if not EMAIL_RE.match(email):
        return "invalid", ["bad syntax"]
    local, domain = email.rsplit("@", 1)
    flags: list[str] = []
    if domain in DISPOSABLE:
        return "invalid", ["disposable"]
    if local in ROLE_PREFIXES:
        flags.append("role inbox")
    if domain in FREE_MAIL:
        flags.append("personal")
    elif company_domain and domain != company_domain and not domain.endswith("." + company_domain):
        flags.append("domain mismatch")
    return "unverified", flags


_resolver = dns.asyncresolver.Resolver()
_resolver.lifetime = 4.0


async def has_mx(domain: str) -> bool:
    cached = db.cache_get_mx(domain)
    if cached is not None:
        return cached
    try:
        answer = await _resolver.resolve(domain, "MX")
        # RFC 7505 "null MX" (a single record pointing at ".") means the domain accepts no mail.
        ok = not all(str(r.exchange) == "." for r in answer)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        # No MX: RFC 5321 allows falling back to an A record.
        try:
            await _resolver.resolve(domain, "A")
            ok = True
        except dns.exception.DNSException:
            ok = False
    except dns.exception.DNSException:
        return True  # timeout etc. -> don't punish the lead for our network trouble; don't cache
    db.cache_put_mx(domain, ok)
    return ok


async def check_email(email: str, company_domain: str | None, network: bool) -> tuple[str, list[str]]:
    status, flags = classify(email, company_domain)
    if status != "unverified" or not network:
        return status, flags
    domain = email.rsplit("@", 1)[1]
    if not await has_mx(domain):
        return "invalid", flags + ["no mail server"]
    return ("risky" if "role inbox" in flags else "valid"), flags


async def check_many(items: list[tuple[str, str | None]], network: bool) -> list[tuple[str, list[str]]]:
    sem = asyncio.Semaphore(20)

    async def one(e: str, d: str | None):
        async with sem:
            return await check_email(e, d, network)

    return await asyncio.gather(*(one(e, d) for e, d in items))
