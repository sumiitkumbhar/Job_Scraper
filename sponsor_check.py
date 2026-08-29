"""
UK Skilled Worker sponsor-licence cross-check — custom add-on for Sumit's fork.

Runs AFTER the normal scrapers (scrape_jobs.py) have written output/all_jobs.json.
Downloads the Home Office's public "Register of licensed sponsors: workers" CSV,
matches each scraped job's company name against it, and estimates whether the
role's stated salary (when present) would clear the UK Skilled Worker visa
salary floor. Writes two files:

  output/sponsor_status.json    — one row per unique company seen in this run
  output/jobs_with_sponsor.csv  — every job in all_jobs.json + sponsor/salary columns

Design notes:
  - The GOV.UK publication page is scraped for the current CSV asset link because
    the Home Office re-uploads the file under a new URL on every refresh (there is
    no stable direct link). If the page layout changes and no CSV link is found,
    the script exits cleanly and leaves previous output files untouched — same
    "preserve on failure" pattern the rest of this repo uses for blocked sources.
  - Matching is a normalized substring match (strip Ltd/Limited/plc/LLP/Group/UK,
    lowercase, collapse whitespace) in both directions, with a short-name guard to
    avoid false positives from very short organisation names. This is a
    *starting point* — always confirm manually before relying on it for a specific
    application; sponsor status can lapse, and a job's listed employer is
    sometimes a recruiter, not the actual sponsor.
  - No API key, no paid service. Only needs network access, which is why this is
    meant to run in GitHub Actions (unrestricted egress) rather than locally in a
    sandboxed environment.

Run manually:
    python sponsor_check.py

Wire into CI: see .github/workflows/sponsor_watch.yml (runs daily, after the
other watchers, and commits the two output files like every other source in
this repo).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
ALL_JOBS_PATH = os.path.join(OUTPUT_DIR, "all_jobs.json")
SPONSOR_STATUS_PATH = os.path.join(OUTPUT_DIR, "sponsor_status.json")
JOBS_CSV_PATH = os.path.join(OUTPUT_DIR, "jobs_with_sponsor.csv")

GOVUK_PUBLICATION_PAGE = (
    "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JobScraperSponsorCheck/1.0; "
        "personal job-search tool; no scraping of gov.uk beyond this one page+CSV)"
    )
}
REQUEST_TIMEOUT = 60

# UK Skilled Worker visa salary thresholds (per Home Office rules in effect
# since 22 Jul 2025). Update these two numbers if the rules change.
SW_GENERAL_THRESHOLD = 41700
SW_NEW_ENTRANT_THRESHOLD = 33400

ORG_SUFFIX_RE = re.compile(
    r"\b(limited|ltd|plc|llp|group|holdings?|uk|the|inc|incorporated|co)\b\.?",
    re.I,
)
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_org(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation, collapse whitespace."""
    if not name:
        return ""
    n = name.lower()
    n = ORG_SUFFIX_RE.sub(" ", n)
    n = NON_ALNUM_RE.sub(" ", n)
    return " ".join(n.split())


def fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def find_csv_url() -> str | None:
    """Scrape the GOV.UK publication page for the current CSV attachment link."""
    try:
        html = fetch(GOVUK_PUBLICATION_PAGE).decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  WARNING: could not load GOV.UK publication page: {e}")
        return None

    # Attachment links look like:
    # https://assets.publishing.service.gov.uk/media/<id>/<filename>.csv
    matches = re.findall(
        r'href="(https://assets\.publishing\.service\.gov\.uk/media/[^"]+?\.csv)"',
        html,
        re.I,
    )
    if not matches:
        print("  WARNING: no .csv attachment link found on the publication page")
        return None
    # Prefer a link whose filename mentions "worker" (the register is sometimes
    # split into multiple CSVs, e.g. an archive); fall back to the first match.
    for m in matches:
        if "worker" in m.lower():
            return m
    return matches[0]


def load_register(csv_url: str) -> list[dict]:
    raw = fetch(csv_url, timeout=120)
    # The Home Office file is sometimes Windows-1252 / has a BOM.
    text = raw.decode("utf-8-sig", "ignore")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    return rows


def org_name_key(row: dict) -> str | None:
    for key in row.keys():
        if "organisation" in key.lower() or "organization" in key.lower():
            return key
    return None


def build_lookup(rows: list[dict]) -> tuple[dict[str, dict], str | None]:
    if not rows:
        return {}, None
    name_key = org_name_key(rows[0])
    if not name_key:
        print(f"  WARNING: could not find an organisation-name column; saw {list(rows[0].keys())}")
        return {}, None
    lookup: dict[str, dict] = {}
    for row in rows:
        raw_name = (row.get(name_key) or "").strip()
        norm = normalize_org(raw_name)
        if len(norm) < 3:
            continue
        # Keep the first occurrence; the register can list a sponsor more than
        # once (e.g. multiple routes) — one confirmed hit is enough for us.
        lookup.setdefault(norm, {"official_name": raw_name, **row})
    return lookup, name_key


def match_company(company: str, lookup: dict[str, dict]) -> dict:
    norm = normalize_org(company)
    if not norm:
        return {"status": "unknown", "matched_name": None}

    # 1. Exact normalized match.
    if norm in lookup:
        return {"status": "confirmed", "matched_name": lookup[norm]["official_name"]}

    # 2. Substring match either direction, guarded against very short names
    #    (e.g. "jll" matching inside an unrelated longer name by accident).
    if len(norm) >= 4:
        for key, row in lookup.items():
            if len(key) < 4:
                continue
            if norm in key or key in norm:
                return {"status": "likely", "matched_name": row["official_name"]}

    return {"status": "not_found", "matched_name": None}


def parse_salary_gbp(salary_text: str | None) -> tuple[int | None, int | None]:
    """Best-effort extraction of a (low, high) annual GBP figure from a free-text
    salary string such as '£28,000 - £32,000 a year' or '£35k/yr'. Returns
    (None, None) if nothing parseable (hourly/day rates, non-GBP, or empty)."""
    if not salary_text:
        return None, None
    text = salary_text.replace(",", "")
    if "£" not in text and "gbp" not in text.lower():
        return None, None
    if re.search(r"\b(hour|hr|day|daily)\b", text, re.I):
        return None, None

    nums = []
    for m in re.finditer(r"£?\s*(\d+(?:\.\d+)?)\s*(k)?", text, re.I):
        val = float(m.group(1))
        if m.group(2):
            val *= 1000
        if val >= 1000:  # ignore stray small numbers (e.g. a bonus %)
            nums.append(val)
    if not nums:
        return None, None
    return int(min(nums)), int(max(nums))


def salary_flag(low: int | None, high: int | None) -> str:
    if low is None:
        return "not stated"
    top = high or low
    if top >= SW_GENERAL_THRESHOLD:
        return "clears general threshold"
    if top >= SW_NEW_ENTRANT_THRESHOLD:
        return "clears new-entrant threshold only"
    return "below both thresholds"


def main() -> int:
    if not os.path.exists(ALL_JOBS_PATH):
        print(f"  No {ALL_JOBS_PATH} yet — run scrape_jobs.py first. Skipping.")
        return 0

    with open(ALL_JOBS_PATH, encoding="utf-8") as f:
        all_jobs_doc = json.load(f)
    jobs = all_jobs_doc.get("jobs", [])
    if not jobs:
        print("  all_jobs.json has no jobs yet. Skipping.")
        return 0

    print("🔎 Checking UK Skilled Worker sponsor register...")
    csv_url = find_csv_url()
    if not csv_url:
        print("  ⛔ Could not locate the register CSV this run; leaving previous sponsor output untouched.")
        return 0
    print(f"  Register source: {csv_url}")

    try:
        rows = load_register(csv_url)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  ⛔ Could not download the register CSV: {e}; leaving previous sponsor output untouched.")
        return 0

    lookup, name_key = build_lookup(rows)
    if not lookup:
        print("  ⛔ Register parsed but no usable organisation names found; leaving previous sponsor output untouched.")
        return 0
    print(f"  Loaded {len(lookup)} unique sponsor organisations (from {len(rows)} register rows).")

    company_status: dict[str, dict] = {}
    enriched_rows = []
    for job in jobs:
        company = job.get("company", "") or ""
        if company not in company_status:
            company_status[company] = match_company(company, lookup)
        status = company_status[company]

        low, high = parse_salary_gbp(job.get("salary"))
        flag = salary_flag(low, high)

        enriched_rows.append({
            "title": job.get("title", ""),
            "company": company,
            "location": job.get("location", ""),
            "sponsor_status": status["status"],
            "sponsor_matched_name": status["matched_name"] or "",
            "salary": job.get("salary", ""),
            "salary_vs_skilled_worker": flag,
            "date_posted": job.get("date_posted", ""),
            "ats": job.get("ats", ""),
            "url": job.get("url", ""),
        })

    counts = {"confirmed": 0, "likely": 0, "not_found": 0, "unknown": 0}
    for s in company_status.values():
        counts[s["status"]] = counts.get(s["status"], 0) + 1

    with open(SPONSOR_STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "register_source": csv_url,
            "register_org_count": len(lookup),
            "company_counts": counts,
            "companies": company_status,
        }, f, indent=2)

    with open(JOBS_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(enriched_rows[0].keys()) if enriched_rows else [])
        writer.writeheader()
        writer.writerows(enriched_rows)

    print(
        f"  ✅ {len(company_status)} companies checked — "
        f"{counts.get('confirmed', 0)} confirmed sponsors, "
        f"{counts.get('likely', 0)} likely matches, "
        f"{counts.get('not_found', 0)} not found."
    )
    print(f"  📄 Saved {SPONSOR_STATUS_PATH} and {JOBS_CSV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
