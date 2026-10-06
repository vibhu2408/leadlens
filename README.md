# LeadLens: lead qualification for SaaSquatch exports

**Scraping gets you a list. LeadLens tells you who to call first, and why.**

LeadLens sits between a lead-generation tool such as [SaaSquatch](https://www.saasquatchleads.com/) and your CRM. You upload a raw export, and it:

1. **Cleans** the file. It maps messy column names automatically ("Owner Name", "Employee Count", "Year Founded" and so on) and normalizes domains, names, and ranges like `$1-5M` or `11-50`.
2. **Deduplicates** leads by email, by domain plus contact, or by fuzzy company name ("ACME PLUMBING INC" matches "Acme Plumbing LLC"). It merges duplicates instead of dropping them, so no data is lost.
3. **Enriches** each company from its own website: on-domain emails, phones, socials, tech stack, hiring signal, and when the site was last updated. It also fills missing emails and phones from what it finds.
4. **Verifies** emails. It checks syntax and flags disposable domains, role inboxes (`info@`), personal mail, domain mismatches, and domains that cannot receive mail (MX lookup, including RFC 7505 null-MX).
5. **Scores** every lead from 0 to 100 against an editable ideal customer profile (ICP), tiers it A to D, and **shows the reason behind every point**.
6. **Drafts** a first-touch email grounded only in that lead's verified facts. It uses an LLM when an API key is set and a template otherwise.
7. **Exports** a HubSpot-ready CSV (including score, tier, and reasons) or a full analysis CSV.

There are two scoring presets:

- **Acquisition targets (ETA / PE).** This is Caprae's own use case. It rewards established, owner-run, lower-middle-market businesses with succession signals: years in business, an owner-held contact, and a stale web presence.
- **Sales outreach.** It rewards reachable decision makers at growing, digitally active companies: hiring, marketing tech in use, and recently updated sites.

> Demo video: _add link_ · Live demo: _add link after deploying_

---

## Quick start (about 2 minutes)

Requires Python 3.11 or newer.

```bash
git clone https://github.com/vibhu2408/leadlens.git
cd leadlens
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 and click **Try with sample data**, or drop in your own CSV.

To get AI-written outreach drafts instead of templates (optional):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Run the tests:

```bash
pytest -q
```

### Docker

```bash
docker build -t leadlens .
docker run -p 8080:8080 -v leadlens-data:/data -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY leadlens
```

### Sample data

`data/sample_leads.csv` has 40 **fictional** businesses in a SaaSquatch-style layout. It deliberately includes the problems real exports have: duplicate rows with different casing, a company listed with and without a website, a disposable email, a malformed email, role inboxes, Gmail-using owners, a franchise, and a non-profit. Every domain uses the reserved `.example` TLD, so no real business is ever contacted. Run the sample with **Enrich & verify online** turned off for an instant offline pass. To see live enrichment, upload a real export.

---

## Why this feature (business rationale)

SaaSquatch already solves *finding* companies. The bottleneck after that is the hour a rep or deal associate spends per list deciding which rows are worth a call: removing duplicates, guessing which emails bounce, opening websites to see whether the business is still active and owner-run. That triage work is unglamorous, repetitive, and exactly what software should do.

LeadLens targets that step:

| Pain after scraping | What LeadLens does |
|---|---|
| The same company appears 2-3 times | Merges duplicates by email, domain + contact, or fuzzy name; fills gaps from the duplicates |
| Bounced emails hurt sender reputation | MX / null-MX check; flags disposable, role, and personal addresses |
| "Is this business even still active?" | Live website check, copyright-year freshness, hiring signal |
| Missing contact info | Pulls on-domain emails and phones from the homepage and contact page |
| Opaque lead scores nobody trusts | Every point is explained in plain English; weights are editable |
| Different teams want different leads | Presets for acquisitions vs. sales; re-scoring is instant, with no re-scraping |
| Getting results into the CRM | HubSpot-format export with score, tier, and reasons in the notes |

**Acquisition-specific signals.** For ETA and PE buyers, the best target is often the business whose owner is ready to transition. Public lists don't say this outright, so LeadLens infers it from proxies: business age, an owner-held contact title, and a website that hasn't been touched in years. A 38-year-old HVAC company whose owner still answers email and whose site was last updated in 2019 ranks above a polished 5-year-old firm run by a VP.

---

## Architecture

```
Browser (vanilla JS SPA)
   │  fetch /api/*
   ▼
FastAPI (uvicorn, async)
   ├── ingest.py    header aliasing, normalization (domains, ranges, names)
   ├── dedupe.py    exact keys + RapidFuzz fuzzy company match, bucketed by (state, person)
   ├── enrich.py    async httpx scraper → BeautifulSoup parsing
   ├── validate.py  syntax / role / disposable / MX (dnspython async)
   ├── scoring.py   explainable weighted ICP model, presets
   ├── outreach.py  LLM drafts (Anthropic SDK) with template fallback
   ├── export.py    HubSpot + full CSV
   └── pipeline.py  background job: dedupe → enrich → verify → score
   ▼
SQLite (WAL)  batches · leads · enrichment_cache (7-day TTL) · mx_cache (3-day TTL)
```

### Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | **Python 3.12, FastAPI, uvicorn** | Async I/O for hundreds of concurrent site fetches; typed request models |
| Scraping | **httpx (async) + BeautifulSoup** | HTTP/1.1 keep-alive pooling, streaming body caps, proxy support via env |
| DNS | **dnspython** async resolver | Non-blocking MX lookups |
| Dedupe | **RapidFuzz** | C++-backed fuzzy matching, about 1 µs per comparison |
| AI | **Anthropic Python SDK** (model configurable via `LEADLENS_MODEL`), low effort, server-side refusal fallback | Short, factual drafts; falls back to templates when no key is set or on error |
| Database | **SQLite in WAL mode** | Zero-ops, concurrent reads during background writes; all SQL lives in `db.py` for a later Postgres swap |
| Frontend | **Vanilla HTML/CSS/JS** (no build step), Inter font | Instant load, nothing to compile, easy to review |
| Container | **Docker** (python:3.12-slim, non-root) | Same artifact locally and in the cloud |

### Data storage

- `batches`: one row per upload, with preset, ICP JSON, progress, and summary stats.
- `leads`: the canonical record (JSON), email status and flags, enrichment JSON, score, tier, reasons, pipeline status, and drafted opener.
- `enrichment_cache`: keyed by domain and **shared across batches** for 7 days. Re-uploading a list or overlapping lists costs no new requests. Only definitive results are cached (ok, blocked, disallowed); transient failures are retried next time.
- `mx_cache`: keyed by email domain for 3 days. DNS timeouts are never cached, so a lead is never penalized for a network problem on our side.

### Performance and robustness

- **One fetch per unique domain**, not per lead. Ten contacts at one company means one scrape.
- A global concurrency cap (12) with connection pooling, an 8-second timeout, a 1.5 MB body cap, and one retry with backoff on 429/5xx.
- **Contact-page hop**: if the homepage has no email or phone, the scraper follows one `/contact` or `/about` link.
- **robots.txt is honored** and the User-Agent is honest. Bot walls and CAPTCHAs (Cloudflare challenge, reCAPTCHA, hCaptcha) are *detected and reported* as "Bot-walled" rather than parsed as junk or evaded.
- **IP restrictions**: setting `HTTPS_PROXY` routes scraping through a proxy pool with no code changes.
- Re-scoring is pure CPU over stored enrichment, so changing the ICP re-ranks thousands of leads in milliseconds.
- Fuzzy dedupe is bucketed by (state, contact), which keeps it near-linear on large files.
- Progress writes are throttled (about every 400 ms) so the UI can poll without contending with the job.

### Hosting and deployment (recommended: Google Cloud Run)

The app is a single stateless-friendly container that serves both the API and the static UI.

```bash
gcloud run deploy leadlens --source . --region us-central1 \
  --allow-unauthenticated --memory 512Mi --cpu 1 \
  --set-env-vars LEADLENS_DB=/data/leadlens.db \
  --set-secrets ANTHROPIC_API_KEY=anthropic-key:latest \
  --min-instances 0 --max-instances 1
```

- **Serverless containers (Cloud Run)** rather than static hosting plus functions. A batch job runs for tens of seconds with many outbound connections, which suits a container better than short-lived functions, and the instance scales to zero when idle.
- **Persistence**: on Cloud Run, mount a Cloud Storage volume at `/data` (or use a Fly.io / Render persistent disk) for SQLite. Keep `max-instances 1` while on SQLite.
- **Scaling path**: replace `db.py` with Postgres (Cloud SQL), move `pipeline.process_batch` onto a queue (Cloud Tasks or Pub/Sub, or Redis + RQ) with worker instances, and put the enrichment cache in Redis/Memorystore. The module boundaries were drawn so that each of these is a single-file change.
- **CI/CD**: GitHub Actions runs `pytest`, then `gcloud run deploy --source .` (Cloud Build builds the Dockerfile) on pushes to `main`.

---

## UX decisions

- **Three-step flow** (Import → Qualify → Export) shown as a stepper. The happy path needs no configuration: drop a file, click Run.
- **The goal comes before settings.** Users pick "Acquisition targets" or "Sales outreach", not a page of weights. Weights stay one click away in **Tune ICP** for power users.
- **Results lead with the answer.** The first tile is "Tier A leads", followed by the cleanup numbers (duplicates merged, reachable emails, gaps filled), so the value of the cleanup is visible.
- **Explainability.** The score badge opens a drawer showing each factor's points out of its maximum, with a sentence explaining it. Reps trust scores they can read.
- **Inline workflow.** Lead status (new, contacted, qualified, disqualified) is set from the table or the drawer and maps to HubSpot Lead Status on export. Disqualified leads drop out of exports.
- **Keyboard triage.** `j`/`k` move through leads, `Enter` opens one, `Esc` closes it.
- **Signal chips** ("38 yrs", "Owner-run", "Hiring", "Site ©2019", "Bot-walled") make a row scannable without opening it.
- **Export preview** shows how many leads will be exported before you download.
- **Accessible defaults**: semantic HTML, focus rings, `aria-pressed` and `aria-sort`, color always paired with text, light and dark themes, and a responsive layout down to phone width.

---

## Ethical data collection

- robots.txt is honored, the scraper identifies itself, and it makes at most two page requests per domain with bounded concurrency.
- CAPTCHAs are reported, never solved or evaded.
- No SMTP probing: it gets sender IPs blocklisted and annoys mail admins. A paid verifier (ZeroBounce, NeverBounce) can plug into `validate.check_email`.
- Only on-domain business emails are harvested from websites.
- AI drafts are instructed to use *only* the supplied facts, never invented ones, and the user always reviews them before sending.

---

## Project layout

```
app/
  main.py        FastAPI routes + static hosting
  pipeline.py    background job orchestration
  ingest.py      CSV parsing / header mapping / normalization
  dedupe.py      deduplication and merge
  enrich.py      website scraping and parsing
  validate.py    email validation
  scoring.py     ICP presets and explainable scoring
  outreach.py    AI / template outreach drafts
  export.py      CSV exports
  db.py          SQLite schema and queries
static/          index.html, styles.css, app.js
data/            sample_leads.csv (fictional)
tests/           pytest suite (parsing, dedupe, validation, scraping parser, scoring)
docs/            submission notes and video script
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/batches` | multipart `file`, `preset`, `enrich`. Starts a batch |
| POST | `/api/batches/sample` | Runs the bundled sample |
| GET | `/api/batches/{id}` | Status, progress, stats |
| GET | `/api/batches/{id}/leads` | Scored leads |
| PUT | `/api/batches/{id}/icp` | Update the ICP and re-score instantly |
| PATCH | `/api/leads/{id}` | Set pipeline status |
| POST | `/api/leads/{id}/opener` | Draft a first-touch email |
| GET | `/api/batches/{id}/export?format=hubspot\|full&tiers=A,B` | Download CSV |

Interactive docs are at `/docs` (Swagger UI).

## What I'd build next

1. Native HubSpot and Salesforce push through their APIs (instead of CSV), with dedupe against existing CRM records.
2. Pluggable verification (ZeroBounce) and people enrichment (LinkedIn via a compliant provider).
3. Learned scoring: feed back which leads converted and fit the weights with logistic regression, keeping per-factor explanations.
4. Scheduled re-enrichment so stale or changed sites resurface leads (for example, "owner just listed the business" or "site went dark").
