"""
Push the scraped, scored, sponsor-checked jobs into a Google Sheet — the
"loop that keeps updating the sheet" piece.

This is the last stage of the pipeline, run after scrape_jobs.py,
sponsor_check.py and us_sponsor_check.py in CI:

    scrape_jobs.py      -->  output/all_jobs.json
    sponsor_check.py    -->  output/sponsor_status.json      (UK)
    us_sponsor_check.py -->  output/us_sponsor_status.json   (USA)
    sheets_sync.py      -->  writes a "Live Jobs Feed" tab in your Google Sheet

Both sponsor-check files are optional — if either hasn't run yet (or a fork
only cares about one region), its column just shows "unknown" instead of
failing the sync.

It owns ONE tab only (default name "Live Jobs Feed", override with the
GOOGLE_SHEET_TAB variable) and fully rewrites it every run — your other
tabs (Dashboard, Ranked Shortlist, Application Pipeline, Apply Tracker,
etc.) are never touched. Two columns on the owned tab, "Status" and
"Notes", are preserved across runs: they're read back from the sheet
before the rewrite and merged in by job URL, so anything you type there
survives the next automated refresh.

Requires (GitHub Actions → Settings → Secrets and variables → Actions):
  Secret    GOOGLE_SERVICE_ACCOUNT_JSON   the full JSON key of a Google
                                           Cloud service account, pasted as-is
  Variable  GOOGLE_SHEET_ID               the spreadsheet ID from its URL
                                           (…/spreadsheets/d/<THIS PART>/edit)
  Variable  GOOGLE_SHEET_TAB              optional; defaults to "Live Jobs Feed"

The target spreadsheet must be shared with the service account's
client_email (found inside the JSON key) as Editor — the service account
has no access otherwise. See SETUP.md for the full one-time walkthrough.

Without GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID set, this script is
a no-op (prints a message and exits 0) so forks that haven't set up Sheets
sync yet are unaffected — same pattern as the optional Pushover/triage_agent
integrations elsewhere in this repo.

Run manually:
    python sheets_sync.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
ALL_JOBS_PATH = os.path.join(OUTPUT_DIR, "all_jobs.json")
SPONSOR_STATUS_PATH = os.path.join(OUTPUT_DIR, "sponsor_status.json")
US_SPONSOR_STATUS_PATH = os.path.join(OUTPUT_DIR, "us_sponsor_status.json")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
PRIORITY_COMPANIES_PATH = os.path.join(SCRIPT_DIR, "priority_companies.json")

DEFAULT_TAB_NAME = "Live Jobs Feed"
MAX_ROWS = 400  # keep the sheet from growing without bound

# Columns the automation owns outright vs. columns a human edits and the
# automation must preserve across runs.
AUTO_COLUMNS = [
    "Fit Score", "Curated Match", "Title", "Company", "Sector Stream",
    "Suggested Roles", "Location", "Sponsor Status", "Licence Route",
    "US H-1B Sponsor", "Salary", "Salary vs Skilled Worker", "Priority Topics",
    "Role Category", "Date Posted", "Source", "URL",
]
USER_COLUMNS = ["Status", "Notes"]
ALL_COLUMNS = AUTO_COLUMNS + USER_COLUMNS
URL_COL_INDEX = AUTO_COLUMNS.index("URL")

ORG_SUFFIX_RE = re.compile(
    r"\b(limited|ltd|plc|llp|group|holdings?|uk|the|inc|incorporated|co)\b\.?", re.I
)
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_org(name: str) -> str:
    if not name:
        return ""
    n = name.lower()
    n = ORG_SUFFIX_RE.sub(" ", n)
    n = NON_ALNUM_RE.sub(" ", n)
    return " ".join(n.split())


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _import_notify():
    """Reuse notify.py's deterministic _fit() scorer instead of duplicating it."""
    spec = importlib.util.spec_from_file_location("notify", os.path.join(SCRIPT_DIR, "notify.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _compile_category_terms(config: dict, key: str) -> list[tuple[str, re.Pattern]]:
    terms = (config.get(key, {}) or {}).get("terms", [])
    out = []
    for pair in terms:
        try:
            name, pattern = pair
            out.append((name, re.compile(pattern, re.I)))
        except (ValueError, re.error):
            continue
    return out


def match_first(text: str, terms: list[tuple[str, re.Pattern]]) -> str:
    for name, rx in terms:
        if rx.search(text):
            return name
    return ""


def match_all(text: str, terms: list[tuple[str, re.Pattern]]) -> str:
    hits = [name for name, rx in terms if rx.search(text)]
    return ", ".join(hits)


def build_rows(jobs: list[dict], sponsor_companies: dict, us_sponsor_companies: dict,
                priority_companies: dict, config: dict, notify_mod) -> list[list[str]]:
    role_terms = _compile_category_terms(config, "role_categories")
    topic_terms = _compile_category_terms(config, "priority_topics")

    rows = []
    for job in jobs:
        title = job.get("title", "") or ""
        company = job.get("company", "") or ""
        body = f"{company} {job.get('description', '')}"
        text = f"{title} {body}"

        fit = notify_mod._fit(title, body)

        norm = normalize_org(company)
        curated = priority_companies.get(norm)
        curated_match = "Yes" if curated else ""
        sector_stream = curated.get("sector_stream", "") if curated else ""
        suggested_roles = curated.get("suggested_roles", "") if curated else ""

        sponsor = sponsor_companies.get(company, {})
        sponsor_status = sponsor.get("status", "unknown")
        sponsor_matched = sponsor.get("matched_name", "") or ""
        licence_route = curated.get("licence_route", "") if curated else ""

        us_sponsor = us_sponsor_companies.get(company, {})
        us_sponsor_status = us_sponsor.get("status", "unknown")
        us_sponsor_count = us_sponsor.get("certified_lca_count", 0) or 0
        us_sponsor_cell = us_sponsor_status
        if us_sponsor_status in ("confirmed", "likely") and us_sponsor_count:
            us_sponsor_cell = f"{us_sponsor_status} ({us_sponsor_count} certified LCA{'s' if us_sponsor_count != 1 else ''})"

        salary = job.get("salary", "") or ""

        rows.append([
            str(fit),
            curated_match,
            title,
            company,
            sector_stream,
            suggested_roles,
            job.get("location", "") or "",
            f"{sponsor_status}" + (f" ({sponsor_matched})" if sponsor_matched and sponsor_matched != company else ""),
            licence_route,
            us_sponsor_cell,
            salary,
            job.get("salary_vs_skilled_worker", "") or "",  # filled in below if present
            match_all(text, topic_terms),
            match_first(title, role_terms),
            job.get("date_posted", "") or "",
            job.get("ats", "") or "",
            job.get("url", "") or "",
        ])
    return rows


def main() -> int:
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    tab_name = os.environ.get("GOOGLE_SHEET_TAB", DEFAULT_TAB_NAME)

    if not sa_json or not sheet_id:
        print("  Google Sheets sync not configured (GOOGLE_SERVICE_ACCOUNT_JSON / "
              "GOOGLE_SHEET_ID not set) — skipping. See SETUP.md to enable it.")
        return 0

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("  ⛔ gspread not installed — add it to requirements.txt (pip install -r requirements.txt).")
        return 1

    if not os.path.exists(ALL_JOBS_PATH):
        print(f"  No {ALL_JOBS_PATH} yet — run scrape_jobs.py first. Skipping.")
        return 0

    all_jobs_doc = _load_json(ALL_JOBS_PATH, {"jobs": []})
    jobs = all_jobs_doc.get("jobs", [])
    if not jobs:
        print("  all_jobs.json has no jobs yet. Skipping.")
        return 0

    sponsor_doc = _load_json(SPONSOR_STATUS_PATH, {"companies": {}})
    sponsor_companies = sponsor_doc.get("companies", {})

    us_sponsor_doc = _load_json(US_SPONSOR_STATUS_PATH, {"companies": {}})
    us_sponsor_companies = us_sponsor_doc.get("companies", {})

    priority_raw = _load_json(PRIORITY_COMPANIES_PATH, {})
    config = _load_json(CONFIG_PATH, {})
    notify_mod = _import_notify()

    print("📊 Building Google Sheet rows...")
    rows = build_rows(jobs, sponsor_companies, us_sponsor_companies, priority_raw, config, notify_mod)

    # Merge in salary-vs-threshold from sponsor_check's per-job CSV if present
    # (sponsor_check.py computes this per-posting, not per-company).
    sponsor_csv_path = os.path.join(OUTPUT_DIR, "jobs_with_sponsor.csv")
    if os.path.exists(sponsor_csv_path):
        import csv
        salary_flag_by_url = {}
        with open(sponsor_csv_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                salary_flag_by_url[r.get("url", "")] = r.get("salary_vs_skilled_worker", "")
        for row in rows:
            url = row[URL_COL_INDEX]
            if url in salary_flag_by_url:
                row[AUTO_COLUMNS.index("Salary vs Skilled Worker")] = salary_flag_by_url[url]

    # Sort: highest fit first, then most recent.
    def sort_key(row):
        try:
            fit = int(row[AUTO_COLUMNS.index("Fit Score")])
        except ValueError:
            fit = 0
        return (-fit, row[AUTO_COLUMNS.index("Date Posted")] or "")

    rows.sort(key=sort_key)
    rows = rows[:MAX_ROWS]

    print(f"  {len(rows)} rows ready (of {len(jobs)} total jobs).")

    creds = Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=str(MAX_ROWS + 10), cols=str(len(ALL_COLUMNS)))

    # Read back existing Status/Notes keyed by URL so a manual edit survives.
    existing = ws.get_all_values()
    preserved: dict[str, list[str]] = {}
    if existing and existing[0] == ALL_COLUMNS:
        url_i = ALL_COLUMNS.index("URL")
        status_i = ALL_COLUMNS.index("Status")
        notes_i = ALL_COLUMNS.index("Notes")
        for r in existing[1:]:
            if len(r) > url_i and r[url_i]:
                preserved[r[url_i]] = [
                    r[status_i] if len(r) > status_i else "",
                    r[notes_i] if len(r) > notes_i else "",
                ]

    final_rows = [ALL_COLUMNS]
    for row in rows:
        url = row[URL_COL_INDEX]
        user_vals = preserved.get(url, ["", ""])
        final_rows.append(row + user_vals)

    ws.clear()
    ws.update(values=final_rows, range_name="A1")
    try:
        ws.freeze(rows=1)
    except Exception:
        pass  # cosmetic only — never fail the run over a freeze-pane call

    print(f"  ✅ Wrote {len(rows)} jobs to tab '{tab_name}' "
          f"({len(preserved)} rows had Status/Notes preserved from before).")
    print(f"  Updated: {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
