# 2-minute video walkthrough: script

Target length 1:45-2:00. Record the screen at 1440×900, with the app running locally and an `ANTHROPIC_API_KEY` set so the AI draft shows.
Before recording, run the sample once so the batch loads instantly, and have a small real SaaSquatch export ready for the live-enrichment shot.

---

**[0:00-0:15] Problem** *(screen: SaaSquatch export open in a spreadsheet)*
> "SaaSquatch is great at finding companies. But a raw export still has duplicates, emails that bounce, and businesses that closed years ago, and someone spends an hour per list deciding who's actually worth a call. I built LeadLens to automate that triage step."

**[0:15-0:35] Import** *(screen: LeadLens import page)*
> "You drop in the CSV. Column names are detected automatically. Then you pick the goal. Caprae sources acquisition targets, so there's an ETA/PE preset that ranks established, owner-run businesses with succession signals. There's also a standard B2B sales preset. Enrichment is on, so it'll check every website and mail server."

*(click Run, and show the progress stages ticking: Dedupe → Enrich → Verify → Score)*

**[0:35-1:05] Results and explainability** *(screen: batch view)*
> "Top line: how many A-tier leads you have, how many duplicates were merged, what percent of emails are actually reachable, and how many missing emails and phones were filled from company websites.
> Every lead gets a 0-100 score, and you can see why." *(open the top lead)* "Thirty-eight years in business, owner-operated, the website hasn't been updated since 2019, decision maker reachable. Each factor shows its points. Reps trust a score they can read."

*(scroll to the Website section: tech, emails found, hiring, bot-walled status)*

**[1:05-1:25] Workflow** *(screen: drawer → Draft email; then Tune ICP)*
> "One click drafts a first-touch email with AI, grounded only in verified facts about this lead. For acquisitions it's written to an owner, so it's respectful and confidential.
> If your thesis changes, say a bigger revenue band or adding a new industry, Tune ICP re-scores everything instantly with no re-scraping."

**[1:25-1:45] Export and tech** *(screen: Export dialog → HubSpot CSV opens)*
> "Export goes straight into HubSpot with score, tier and the reasons in the notes, and invalid or disqualified leads are skipped automatically.
> Under the hood: FastAPI with an async scraper that respects robots.txt and detects CAPTCHAs instead of evading them, MX-record email checks, fuzzy dedupe, and a SQLite cache, so a domain is only scraped once a week across every batch. It ships as one Docker container to Cloud Run."

**[1:45-2:00] Close**
> "The point isn't more leads. It's fewer, better ones, with the reasoning attached, so the team spends its time on conversations. Thanks for watching."

---

### Recording tips
- Use OBS or Loom; 1080p, with the mouse highlight on.
- Hide the browser bookmarks bar and any company/work tabs.
- Zoom to 110% so the text is readable on a small player.
