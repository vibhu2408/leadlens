"""First-touch outreach drafts grounded in the lead's actual signals.

Uses an LLM (Anthropic API) when ANTHROPIC_API_KEY is
available, and falls back to a deterministic template otherwise, so the tool
works fully offline and the AI piece is an upgrade rather than a dependency.
"""
from __future__ import annotations

import os
from datetime import date

import anthropic

MODEL = os.environ.get("LEADLENS_MODEL", "claude-opus-5-5")

SYSTEM = """You write first-touch cold emails for a small team. Rules:
- Under 90 words in the body. Plain text, no markdown, no emojis.
- Open with one specific, true observation about the company drawn ONLY from the facts given. Never invent facts, numbers, or names.
- One clear, low-friction ask (a 15-minute call).
- Respectful and direct; no hype, no flattery, no fake familiarity.
- Output exactly: first line "Subject: <subject under 8 words>", a blank line, then the body, then the sender sign-off "{sender}"."""

GOALS = {
    "acquisition": "The sender is a long-term operator-investor exploring buying and growing a well-run, established business. "
                   "The reader is the owner. Be sensitive: owners care about legacy, employees, and confidentiality.",
    "sales": "The sender sells a B2B product/service described below to this company. The reader is a decision maker.",
}


def _facts(lead: dict) -> str:
    d, e = lead["data"], lead.get("enrichment") or {}
    lines = [
        f"Company: {d.get('company')}", f"Website: {d.get('domain')}", f"Industry: {d.get('industry')}",
        f"Location: {', '.join(x for x in (d.get('city'), d.get('state')) if x)}",
        f"Founded: {d.get('founded')}", f"Employees: {d.get('employees_raw') or d.get('employees')}",
        f"Contact: {d.get('contact_name')} ({d.get('title')})",
        f"Website title: {e.get('title')}", f"Website description: {e.get('description')}",
        f"Tech on site: {', '.join(e.get('tech') or []) or 'none detected'}",
        f"Hiring: {'yes' if e.get('hiring') else 'no signal'}",
        f"Why this lead scored well: " + "; ".join(r["why"] for r in lead.get("reasons", []) if r["value"] >= 0.6),
    ]
    return "\n".join(l for l in lines if not l.endswith(": None") and not l.endswith(": "))


def template_opener(lead: dict, preset: str, sender: str) -> str:
    d, e = lead["data"], lead.get("enrichment") or {}
    first = (d.get("contact_name") or "there").split(" ")[0]
    company = d.get("company") or d.get("domain") or "your company"
    years = f" for {date.today().year - d['founded']} years" if d.get("founded") else ""
    where = f" in {d['city']}" if d.get("city") else ""
    possessive = company + ("'" if company.endswith("s") else "'s")
    if preset == "acquisition":
        return (
            f"Subject: {possessive} next chapter\n\n"
            f"Hi {first},\n\n"
            f"{company} has served customers{where}{years}, which is rare. I work with a long-term, operator-led firm "
            f"that partners with established owners who are thinking about succession, without flipping the business "
            f"or disrupting the team.\n\n"
            f"Would you be open to a confidential 15-minute call to see if there is a fit, now or down the road?\n\n{sender}"
        )
    hook = "you're hiring" if e.get("hiring") else f"{company} is growing{where}"
    return (
        f"Subject: Quick idea for {company}\n\n"
        f"Hi {first},\n\n"
        f"Saw that {hook}. Teams at that stage usually hit the same wall: more leads than time to qualify them. "
        f"We help companies like yours focus reps on the accounts most likely to close.\n\n"
        f"Worth a 15-minute call next week?\n\n{sender}"
    )


def generate_opener(lead: dict, preset: str, sender: str = "[Your name]", offering: str = "") -> tuple[str, str]:
    """Returns (text, source) where source is 'ai' or 'template'."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return template_opener(lead, preset, sender), "template"
    prompt = f"Goal: {GOALS.get(preset, GOALS['sales'])}\n"
    if offering:
        prompt += f"What the sender offers: {offering}\n"
    prompt += f"\nFacts about the lead (use only these):\n{_facts(lead)}"
    try:
        client = anthropic.Anthropic(timeout=45.0)
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM.format(sender=sender),
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low"},  # short, factual writing: low effort is enough
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIError:
        return template_opener(lead, preset, sender), "template"
    if resp.stop_reason == "refusal":
        return template_opener(lead, preset, sender), "template"
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return (text, "ai") if text else (template_opener(lead, preset, sender), "template")
