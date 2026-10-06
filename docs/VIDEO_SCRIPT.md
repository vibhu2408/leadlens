# 2-minute video script

**Setup:** fresh start (`rm -f leadlens.db && uvicorn app.main:app --port 8000`), a normal Chrome window at 110% zoom, no work tabs open. Record with Loom (screen + mic).

| Time | Screen | Say |
|---|---|---|
| 0:00-0:15 | SaaSquatch: search results, then "No people data found" | "SaaSquatch finds companies fast, but the export still needs work: phones and emails are often N/A, and nothing tells you who to call first. I built LeadLens for that step." |
| 0:15-0:30 | LeadLens import: upload `saasquatch_texas_city_energy.csv`, choose **Sales outreach**, keep **Enrich** on, click **Run** | "I upload the raw SaaSquatch export as-is. Columns are detected automatically. I pick a goal, and LeadLens cleans, dedupes, enriches, verifies and scores every lead." |
| 0:30-0:50 | Results: point at **Gaps filled: 6** and the phones in the Contact column; open **Tara Energy** | "In two seconds it scraped each company's website, respecting robots.txt, and filled six gaps SaaSquatch had as N/A. It also flags that this email is a role inbox, which is risky for cold outreach, and picks up signals like hiring and tech stack." |
| 0:50-1:15 | **+ New import** → choose **Acquisition targets**, switch **Enrich off** → **Try with sample data**; open the top A-tier lead | "For Caprae's own use case, acquisitions, there's an ETA/PE preset. This 38-year-old owner-run HVAC company scores 91. Every point is explained, so reps and deal teams can trust the ranking. Duplicates were merged automatically." |
| 1:15-1:30 | Click **Draft email**, then **Tune ICP** and change the revenue range → **Apply** | "One click drafts a first-touch email from verified facts only. If the thesis changes, Tune ICP re-scores everything instantly, with no re-scraping." |
| 1:30-1:45 | **Export**, choose CRM import (HubSpot), click Download | "Export is HubSpot-ready, with score, tier and reasons included, and invalid or disqualified leads are skipped." |
| 1:45-2:00 | The README on GitHub | "Stack: FastAPI, an async scraper, MX email checks, fuzzy dedupe, and SQLite with a 7-day cache, shipped as one Docker container. The goal isn't more leads, it's better ones with the reasoning attached." |

**Tips:** do one practice run first. If you go over 2:00, cut the Tune ICP step.
