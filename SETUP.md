# Your planning + PropTech job tracker — setup

This bundle turns [ScottCoffin/Job_Scraper](https://github.com/ScottCoffin/Job_Scraper) into a
personal, self-running tracker for UK/Ireland spatial-planning and PropTech
roles, with three custom additions on top of the stock repo:

1. **`scoring_profile.json`** — a deterministic fit-scorer calibrated to your
   CV (RTPI Licentiate + MSc Spatial Planning + your two AI/PropTech
   projects). No API key needed — plain keyword/regex scoring, tested
   against 5 example postings (see "How the scoring was checked" below).
2. **`sponsor_check.py`** + **`sponsor_watch.yml`** — a daily job that
   downloads the UK Home Office's public *Register of licensed sponsors:
   workers*, matches it against every company the scraper finds, and
   estimates whether each posting's salary (when listed) would clear the
   Skilled Worker visa floor (£41,700 general / £33,400 new-entrant, in
   effect since July 2025).
3. **`sheets_sync.py`** + **`sheets_sync.yml`** — the loop you asked for:
   every hour it rewrites a **"Live Jobs Feed"** tab in a Google Sheet you
   own, ranked by fit score, with sponsor status and salary-threshold flags
   already filled in. Anything you type in that tab's **Status** or
   **Notes** columns survives the next refresh — the automation reads them
   back and merges them in before rewriting, matched by job URL.

Also included: **`priority_companies.json`** — built from the **306-company
shortlist you uploaded** (`Ranked Sponsor-Licensed Companies Relevant to
Your Resumes`), cross-checked against the **24 Aug 2026 Home Office
register** you also uploaded. Every job the scraper finds gets checked
against it, so a posting from a company already on your shortlist shows
"Curated Match: Yes" plus its sector stream and suggested roles straight
from your own research. `config.json`'s priority-employer list now uses
your real 107 licence-confirmed companies (score ≥ 60) instead of my
earlier guessed list.

Everything runs free on GitHub's own servers — nothing to install, no
computer that has to stay on, no Ollama/local LLM.

## Part A — the scraper + sponsor check (10 minutes, one time)

1. **Fork the repo.** Go to
   [github.com/ScottCoffin/Job_Scraper](https://github.com/ScottCoffin/Job_Scraper)
   and click **Fork** (top right). Free GitHub account needed.

2. **Add these files to your fork**, keeping the same paths:
   - `config.json` → repo root
   - `scoring_profile.json` → repo root
   - `priority_companies.json` → repo root
   - `sponsor_check.py` → repo root
   - `sheets_sync.py` → repo root
   - `requirements.txt` → repo root (**replaces** the existing one — adds `gspread` + `google-auth`)
   - `.github/workflows/sponsor_watch.yml` → that exact folder path
   - `.github/workflows/sheets_sync.yml` → that exact folder path

   Easiest way with no git needed: **Add file → Upload files** on your
   fork's GitHub page, drag everything in (GitHub keeps the
   `.github/workflows/` path if you drag the folder itself), then
   **Commit changes**.

3. **Turn on GitHub Pages** (hosts the `triage.html` dashboard):
   Settings → Pages → Source: **Deploy from a branch** → Branch **main**,
   folder **/(root)** → Save. Live in ~1 minute at
   `https://YOUR-USERNAME.github.io/Job_Scraper/triage.html`.

4. **Turn on Actions:**
   - Actions tab → "I understand my workflows, enable them."
   - Settings → Actions → General → Workflow permissions → **Read and write
     permissions** → Save.
   - Settings → Secrets and variables → Actions → **Variables** tab → add
     `ENABLE_DATA_COMMITS` = `true`.

5. **Run it the first time.** Actions tab → LinkedIn, Indeed, Google Jobs,
   HiringCafe watchers → **Run workflow** (tick **backfill** on LinkedIn and
   Indeed's first run for a real historical window). Then run
   **Sponsor & Salary Check**.

## Part B — the Google Sheet loop (about 10 more minutes, one time)

This is the part that needs a small Google Cloud setup — there's no way
around a one-time credential step for an unattended script to write to your
Sheets. It only has to be done once.

1. **Create the Sheet.** Make a new Google Sheet (or reuse the one you
   already have with your Ranked Shortlist / Application Pipeline tabs —
   the automation only ever touches its own tab, see below). Copy its ID
   out of the URL: `https://docs.google.com/spreadsheets/d/`**`THIS-PART`**`/edit`.

2. **Create a Google Cloud service account** (a robot account just for this
   script — it's free):
   - Go to [console.cloud.google.com](https://console.cloud.google.com) →
     create a project (any name, e.g. "job-tracker").
   - **APIs & Services → Library** → search **Google Sheets API** → **Enable**.
   - **APIs & Services → Credentials** → **Create Credentials → Service
     account** → give it any name → **Create and continue** → skip the
     optional role/access steps → **Done**.
   - Click the service account you just made → **Keys** tab → **Add key →
     Create new key → JSON** → it downloads a `.json` file. Keep it safe —
     it's a password.
   - Open that file and copy the `client_email` value (looks like
     `job-tracker@your-project.iam.gserviceaccount.com`).

3. **Share your Sheet with the robot.** Open your Google Sheet → **Share**
   → paste that `client_email` → give it **Editor** → Send (untick "notify
   people", it's a robot).

4. **Add the credentials to your fork:**
   - Settings → Secrets and variables → Actions → **Secrets** tab → **New
     repository secret** → name it `GOOGLE_SERVICE_ACCOUNT_JSON` → paste
     the **entire contents** of the downloaded JSON file → Add secret.
   - Same page → **Variables** tab → **New repository variable** → name
     `GOOGLE_SHEET_ID` → paste the Sheet ID from step 1.
   - (Optional) Add variable `GOOGLE_SHEET_TAB` if you want the tab called
     something other than the default `Live Jobs Feed`.

5. **Run it once manually:** Actions tab → **Google Sheet Sync** → **Run
   workflow**. Open your Sheet — you should see a new `Live Jobs Feed` tab,
   ranked by fit score, with sponsor status and salary-threshold columns
   filled in.

From here it's fully automatic: LinkedIn/Indeed/Google Jobs/HiringCafe run
hourly, Sponsor & Salary Check runs daily at 06:05 UTC, and Google Sheet
Sync runs hourly at :50 past — always reading the latest scrape and sponsor
data. Nothing to restart, nothing to keep open.

**Your other tabs are never touched.** The sync only owns the `Live Jobs
Feed` tab; it rewrites that tab completely on every run but never looks at
Dashboard, Ranked Shortlist, Application Pipeline, Apply Tracker, or
anything else in the same spreadsheet.

## Optional: phone alerts

For a push the moment a strong match appears (Pushover, a one-time ~£4
app), follow **Step 6** in the main
[README](https://github.com/ScottCoffin/Job_Scraper#step-6--phone-notifications-optional).
It automatically uses your `scoring_profile.json` — nothing else to set up.

## How the scoring was checked

`scoring_profile.json` was run through the repo's actual scoring function
(`notify.py`'s `_fit()`) against 5 example postings before delivery:

| Example posting | Score | Why |
|---|---|---|
| Graduate Planning Technology Consultant (RTPI + AI/compliance product) | 100 | Exact target profile |
| Graduate Town Planner (pure planning consultancy, no tech) | 70 | Strong, but correctly below the ideal combined role |
| Solutions Consultant (generic SaaS, no planning domain) | 38 | Real overlap but not domain-specific |
| Site Engineer (construction/civil) | 0 | Explicitly the track you're moving away from |
| Software Engineer (unrelated fintech) | 0 | No planning/PropTech domain content |

`sheets_sync.py`'s row-building and Status/Notes-preservation logic was
also dry-run end to end against mocked data before delivery, including
confirming a manually-typed Status/Notes value survives a rewrite.

If real postings don't feel right, `fit_terms`/`poor_fit_terms` in
`scoring_profile.json` are the knobs — edit and commit, no code changes.

## What's already tuned in `config.json`

- **Locations:** United Kingdom (London, Reading/Thames Valley called out),
  plus Ireland, Netherlands and Germany.
- **Roles:** graduate/assistant planner, spatial/town/urban planning,
  development management, planning technology, PropTech, GIS, compliance
  (Approved Document B / building regulations), solutions consulting.
  Director/principal/senior-only titles excluded.
- **Priority employers:** your own 107 licence-confirmed companies (score
  ≥ 60 in your Ranked Shortlist) — Arup/WSP/AECOM/Avison Young/Bidwells/
  Colliers/CBRE and the rest of your Property & Real Estate, Construction &
  Infrastructure, Planning & Development and PropTech streams. Edit
  `employers.priority` any time; `priority_companies.json` (all 301
  companies, any score) drives the "Curated Match" column on every scraped
  job, not just the priority digest.

## A caution on the sponsor-check and curated-match layers

Both are a starting point, not a guarantee: sponsor status can lapse
between register updates, listed employers are sometimes recruiters rather
than the hiring company, and name matching isn't perfect. Always re-verify
a specific company on the official register before relying on it for a
real application decision — and remember your own shortlist notes "Role
Sponsorship Confirmed: No — verify vacancy" for a reason: a company holding
a licence doesn't mean *this specific role* will be sponsored.
