"""
Scrape the niche/country job boards from the Sheet's "Additional Job Sources"
tab that python-jobspy doesn't support (see config below) — the automated
half of a deliberately hybrid approach: sources that turn out to be easy to
scrape get automated here, sources that don't (anti-bot, JS-only search,
unclear URL) stay as manual links in the Sheet tab until someone verifies
them live and flips them on.

    additional_sources_watch.py  -->  merges into output/all_jobs.json,
                                       same as every other watcher

Design notes (read before editing):

- This script is standalone and imports scrape_jobs.py dynamically (same
  trick sheets_sync.py uses for notify.py) purely to REUSE its keyword/
  location filters, work-arrangement classifier, and the save_jobs_output()
  pipeline (pharma filter, all_jobs.json accumulator, digest generation,
  Pushover notify). It does not modify scrape_jobs.py at all.

- Every source in additional_sources.json is scraped independently and
  wrapped in its own try/except: one broken/redesigned site must never take
  the rest of the run down with it. A source that raises or returns 0
  candidates just contributes nothing this run -- it does NOT delete
  anything from all_jobs.json (the accumulator there is additive/time-pruned
  only, see scrape_jobs.py's _merge_into_all_jobs).

- Two fetch/extract engines, picked per-source via `"engine"` in
  additional_sources.json:
    - `"stdlib"` (default) -- plain `urllib` fetch + `extract_jobs_heuristic()`:
      finds links whose href looks like a job-detail URL (/job/, /jobs/,
      /vacature/, /vacancy/, /career/, /position/ + a slug), then pulls
      title/company/location/salary/date out of that link's containing
      block with light regex heuristics. For the sites that are plain
      server-rendered HTML with no anti-bot (confirmed for PlanningJobs.com,
      Proptech Jobs, IamExpat) this is enough -- no browser needed.
    - `"scrapling"` -- for sites plain urllib can't reach at all (confirmed
      anti-bot/blocked: Xing; likely heavy JS/anti-bot: StepStone.de;
      JS-only search results: LMRE). Uses Scrapling
      (github.com/D4Vinci/Scrapling, free/BSD-3) via `_fetch_via_scrapling()`
      + `extract_jobs_scrapling()`, which reads precise CSS selectors from
      each source's `"selectors"` config rather than the regex heuristic.
  Neither engine has real per-site selectors verified live yet for most
  sources -- see each source's `"status"` in additional_sources.json.

- Run `python additional_sources_watch.py --dry-run` (or trigger the
  workflow with dry_run=true) before ever flipping a new source to
  "enabled": true in additional_sources.json. It fetches and parses
  normally but never writes output or touches the Sheet -- just prints a
  per-source match count and a few sample titles so you can eyeball whether
  the heuristic actually found real job cards before trusting it on a
  schedule.

Run manually:
    python additional_sources_watch.py                # full run, all enabled sources
    python additional_sources_watch.py --dry-run       # parse + report only, no writes
    python additional_sources_watch.py --source lmre   # just one source (add --dry-run too if testing)
"""

from __future__ import annotations

import html
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "additional_sources.json")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

REQUEST_TIMEOUT = 15
REQUEST_DELAY = 1.5  # be extra polite to small boards that aren't built for scraping traffic
MAX_RETRIES = 2

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

# Candidate job-detail link patterns across English/Dutch/German/Irish boards.
# The second alternative was added after the first live dry-run (5 Sep 2026)
# found PlanningJobs.com returning 0 candidates: its real job URLs are plain
# root-level slugs like "/senior-officer-principal-planning-officer-pjcom2826"
# with no "/job/"-style path segment at all -- only a "-pjcomNNNN" ID suffix
# (confirmed live via browser recon). Kept as a targeted addition rather than
# a generic "-id\d+$" pattern to avoid matching unrelated slugs on other sites.
JOB_LINK_RE = re.compile(
    r"/(jobs?|vacatures?|vacancy|vacancies|career|careers|position|positions)/[^/\s\"'#?]+"
    r"|/[^/\s\"'#?]*-pjcom\d+(?:[/?#]|$)",
    re.IGNORECASE,
)
# Some boards (confirmed live for Proptech Jobs, 5 Sep 2026) wrap an entire
# job card -- logo, title, company, meta -- in a single <a>, with the real
# title and company each in their own nested heading tag. When present,
# these give a far cleaner title/company than flattening the whole card to
# text (see extract_jobs_heuristic).
HEADING_RE = re.compile(r"<h[1-4][^>]*>(.*?)</h[1-4]>", re.IGNORECASE | re.DOTALL)
SALARY_RE = re.compile(
    r"[£€$]\s?\d[\d,.]*\s?k?"
    r"(?:\s?(?:-|to)\s?[£€$]?\s?\d[\d,.]*\s?k?)?"
    r"(?:\s?\+\s?[a-z ]{2,20})?"  # trailing extras like "+ car extra", "+ bonus"
    r"(?:\s?(?:per|/)\s?(?:year|yr|annum|hour|hr))?",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"(?:posted\s+)?(\d+\s+(?:day|hour|week|month)s?\s+ago|today|yesterday|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w+\s+\d{4})",
    re.IGNORECASE,
)
NOISE_LINK_TEXT = {
    "apply", "apply now", "read more", "view job", "view", "details",
    "more info", "learn more", "see more", "job details", "view details",
}


def _import_scrape_jobs():
    """Dynamically load scrape_jobs.py to reuse its filters/helpers without
    modifying it or duplicating config-driven keyword/location logic."""
    spec = importlib.util.spec_from_file_location(
        "scrape_jobs", os.path.join(SCRIPT_DIR, "scrape_jobs.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_config() -> list[dict]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f).get("sources", [])


def fetch_html(url: str) -> str | None:
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(1.0 + attempt)
    print(f"    ⚠️  fetch failed after {MAX_RETRIES + 1} attempt(s): {last_err}")
    return None


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# Rough signal that a candidate container actually reaches a field label
# (see _find_container docstring) -- deliberately separate from _LABEL_WORDS
# below (which lacks colons and is used for the precise value-extraction
# regex) so this file's read order doesn't matter.
_CONTAINER_LABEL_HINTS = ("company:", "employer:", "location:", "based in", "salary:")


def _find_container(page: str, link_start: int, link_end: int) -> str:
    """
    Walk outward from a job-link's position to nearby block tags (li/article/
    div/tr) so we have title/company/location/salary/date text to search
    within, without a full DOM parse. Cheap and imprecise by design -- see
    module docstring on upgrading to real selectors.

    Some sites put company/location in a SIBLING block one level further out
    than the link's own immediate wrapper (confirmed live on PlanningJobs.com,
    5 Sep 2026: the title <a> sits inside a small "premium-detail" div whose
    OWN closing tag comes right after it -- one full sibling
    <div class="row"> containing the "Company:"/"Salary:"/"Location:" h5s
    comes right after that, before any unrelated content). So instead of
    always stopping at the very first closing li/article/div/tr tag after the
    link, try progressively wider windows and keep the smallest one that
    covers the MOST recognizable field labels (not just the first window with
    ANY label -- an early window can contain "Company:" alone and stop before
    reaching "Salary:"/"Location:" a little further out, which is exactly the
    bug this replaced: the first version stopped as soon as it saw one hint
    and returned before the label group was fully captured). Fall back to the
    narrowest window if none contain any label at all, rather than guessing
    wider than necessary.

    SECOND bug found live, 5 Sep 2026 (Deliverable-5 go-live run #4): the first
    widening fix above only tried the first 5 closing tags (closes[:5]), which
    was still too narrow. Each field on PlanningJobs.com is its own
    <div class="col-12"> that ALSO wraps a tiny icon <div class="premium-inf-icn">
    which closes immediately -- so each field consumes 2 closing div tags, not
    1. By the 5th closing div the window had only grown enough to cover
    Company + Salary; Location (the 3rd field) never got a chance to be
    considered, so it kept silently falling back to the source's country.
    Raised the window to closes[:20] so 3+ label fields, each costing 2 closes,
    fit comfortably with room to spare.
    """
    block_open_re = re.compile(r"<(li|article|div|tr)\b", re.IGNORECASE)
    block_close_re = re.compile(r"</(li|article|div|tr)>", re.IGNORECASE)

    # Search backward for the nearest opening block tag before the link.
    start = 0
    for m in block_open_re.finditer(page, 0, link_start):
        start = m.start()

    closes = list(block_close_re.finditer(page, link_end, link_end + 4000))
    if not closes:
        return page[start:link_end + 1500]

    best_candidate = page[start:closes[0].end()]
    best_hint_count = 0
    for close_m in closes[:20]:
        candidate = page[start:close_m.end()]
        candidate_lower = _strip_tags(candidate).lower()
        hint_count = sum(1 for hint in _CONTAINER_LABEL_HINTS if hint in candidate_lower)
        if hint_count > best_hint_count:
            best_candidate = candidate
            best_hint_count = hint_count
    return best_candidate


def extract_jobs_heuristic(page_html: str, base_url: str) -> list[dict]:
    """Best-effort extraction: find job-detail links, pull nearby text for
    title/company/location/salary/date. See module docstring."""
    candidates: dict[str, dict] = {}
    anchor_re = re.compile(
        r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in anchor_re.finditer(page_html):
        href, inner = m.group(1), m.group(2)
        if not JOB_LINK_RE.search(href):
            continue
        link_text = _strip_tags(inner)
        if not link_text or link_text.lower() in NOISE_LINK_TEXT or len(link_text) < 6:
            continue
        full_url = urllib.parse.urljoin(base_url, href.split("#")[0])
        if full_url in candidates:
            continue

        # Prefer real heading text over the fully-flattened link text when
        # the anchor wraps a whole card (see HEADING_RE comment above) --
        # first heading is almost always the job title, a second one right
        # after it is very often the company name.
        headings = [h for h in (_strip_tags(h) for h in HEADING_RE.findall(inner)) if h]
        title = headings[0] if headings else link_text
        company_from_heading = headings[1] if len(headings) > 1 else ""

        container = _find_container(page_html, m.start(), m.end())
        container_text = _strip_tags(container)

        # Find the date first and mask it out before looking for salary --
        # otherwise salary's "+ trailing extras" (e.g. "+ car allowance") can
        # greedily run on into a following "Posted 3 days ago" and swallow
        # the word "Posted", which then breaks the date's own removal later
        # in _guess_company_and_location (replace() needs an exact substring).
        date_m = DATE_RE.search(container_text)
        salary_search_text = container_text
        if date_m:
            salary_search_text = container_text[:date_m.start()] + container_text[date_m.end():]
        salary_m = SALARY_RE.search(salary_search_text)

        candidates[full_url] = {
            "title": title,
            "url": full_url,
            "company": company_from_heading,  # "" when no second heading -- scrape_source() falls back to guessing
            "salary": salary_m.group(0).strip() if salary_m else "",
            "date_posted": date_m.group(0).strip() if date_m else "",
            "_container_text": container_text[:400],  # kept for company/location guesswork below
        }
    return list(candidates.values())


# Labels commonly used to mark these fields on job-board cards -- kept as a
# single list so the "stop at the next label" lookahead in _extract_labeled
# knows about all of them, however they're ordered on a given site.
_LABEL_WORDS = ("company", "employer", "location", "based in", "salary", "posted")


def _extract_labeled(text: str, label_words: tuple[str, ...]) -> str:
    """
    Look for an explicit "Label: value" pattern (confirmed live on
    PlanningJobs.com, 5 Sep 2026: "Company: Hyndburn Borough Council",
    "Location: Accrington, North West") and return the value, stopping at
    whichever of _LABEL_WORDS' labels comes next so one field's value never
    swallows the next label's. Returns "" if none of label_words appear.
    Far more reliable than the term-search guess below when a site's markup
    actually labels its fields like this -- which is common.
    """
    stop = "|".join(re.escape(w) for w in _LABEL_WORDS)
    for label in label_words:
        m = re.search(
            rf"\b{re.escape(label)}\s*:\s*(.+?)(?=\s*\b(?:{stop})\s*:|$)",
            text, re.IGNORECASE,
        )
        if m:
            val = m.group(1).strip(" ,.-")
            if val:
                return val
    return ""


def _guess_company_and_location(raw: dict, target_terms: list[str]) -> tuple[str, str]:
    """
    First choice: explicit "Company:"/"Location:"-style labels via
    _extract_labeled (see its docstring). Falls back to a much rougher
    guess when a site's markup doesn't label fields this way: look for one
    of the configured target-location terms (config.json ->
    location_filter.terms, e.g. "london", "dublin", "amsterdam") inside the
    container text, and use the text immediately around the title as a
    company guess. Both are best-effort placeholders until per-source
    selectors are captured -- never load-bearing for the keyword filter,
    only for the location filter and for display.
    """
    text = raw.get("_container_text", "")

    labeled_company = _extract_labeled(text, ("company", "employer"))
    labeled_location = _extract_labeled(text, ("location", "based in"))
    if labeled_company or labeled_location:
        return labeled_company, labeled_location

    text_lower = text.lower()
    location = ""
    location_span_text = ""
    for term in target_terms:
        # Word-boundary match only -- a plain substring search would let "uk"
        # match inside "Ramboll UK Limited" and then grab company text into
        # the location guess. \b keeps it to the real standalone word/phrase.
        term_m = re.search(r"\b" + re.escape(term) + r"\b", text_lower)
        if not term_m:
            continue
        idx = term_m.start()
        # Small fixed window around the match, trimmed to punctuation --
        # deliberately narrow rather than clever, see _guess_company_and_location
        # docstring: a wider "grab the whole nearby capitalized phrase" heuristic
        # ends up swallowing unrelated company text more often than not.
        location_span_text = text[max(0, idx - 15):idx + len(term) + 15].strip()
        location = text[idx:idx + len(term)]
        break

    # Company guess: strip out everything we've already identified (title,
    # salary, date, location span), then take the first short remaining chunk.
    remainder = text.replace(raw.get("title", ""), "")
    if raw.get("salary"):
        remainder = remainder.replace(raw["salary"], "")
    if raw.get("date_posted"):
        remainder = remainder.replace(raw["date_posted"], "")
    if location_span_text:
        remainder = remainder.replace(location_span_text, "")
    elif location:
        remainder = remainder.replace(location, "")
    remainder = re.sub(r"\s+", " ", remainder).strip(" -|,+")
    company = ""
    for chunk in re.split(r"[|•\-–]", remainder):
        chunk = chunk.strip(" ,")
        if 2 < len(chunk) < 60:
            company = chunk
            break
    return company, location


def _fetch_via_scrapling(url: str):
    """
    Fetch a page through Scrapling's StealthyFetcher for sources that plain
    urllib can't reach (confirmed anti-bot: Xing 403'd outright; likely
    heavy JS/anti-bot: StepStone.de timed out; JS-only search results:
    LMRE). Only imported lazily, here, so sources that don't need it never
    require the dependency at all.

    IMPORTANT / not yet verified end-to-end: this sandbox's network policy
    blocks installing `scrapling` here (pip gets a 403 specifically on
    scrapling's PyPI page, even though pypi.org itself is reachable and
    other packages install fine -- looks like a policy block on this
    package specifically, not a real absence from PyPI). So this function
    is written to match Scrapling's documented README API exactly
    (`from scrapling.fetchers import StealthyFetcher`, `StealthyFetcher.fetch(url)`,
    `.css()`/`.xpath()` on the result) but has NOT been run against a real
    site from this environment. GitHub Actions runners have normal
    unrestricted network access, so `pip install "scrapling[fetchers]"` +
    `scrapling install` should work fine there -- just budget for the first
    live run on a new "engine": "scrapling" source to possibly need a small
    fix if anything about the API surface has moved since the README was
    last fetched (4 Sep 2026).
    """
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        print("    ⛔ scrapling not installed -- add scrapling[fetchers] to "
              "requirements.txt and run `scrapling install` in the workflow "
              "before this step. Skipping this source.")
        return None
    try:
        return StealthyFetcher.fetch(url)
    except Exception as e:  # noqa: BLE001 -- unverified API, fail this source only
        print(f"    ⚠️  scrapling fetch failed: {e}")
        return None


def extract_jobs_scrapling(page, selectors: dict, base_url: str) -> list[dict]:
    """
    Precise extraction for a "engine": "scrapling" source, using the
    per-source CSS selectors in additional_sources.json (job_card/title/
    company/location/date_posted/salary/url, each a CSS selector string --
    url and the text fields can use a `::text`/`::attr(href)` parsel-style
    suffix same as Scrapling's README examples). Returns [] with a clear
    log line if `selectors` is None/empty -- that's expected until someone
    has looked at the site's real HTML and filled them in; it's not an
    error condition and must never crash the run.
    """
    if not selectors or not selectors.get("job_card"):
        print("    ⚠️  no selectors configured yet for this scrapling source -- "
              "skipping until captured live (see additional_sources.json)")
        return []

    candidates = []
    try:
        cards = page.css(selectors["job_card"])
    except Exception as e:  # noqa: BLE001 -- bad/stale selector, don't crash the run
        print(f"    ⚠️  selector error on job_card ({selectors['job_card']!r}): {e}")
        return []

    for card in cards:
        def field(key, default=""):
            sel = selectors.get(key)
            if not sel:
                return default
            try:
                val = card.css(sel).get()
            except Exception:
                return default
            return (val or default).strip() if isinstance(val, str) else default

        title = field("title")
        if not title:
            continue
        href = field("url")
        full_url = urllib.parse.urljoin(base_url, href) if href else ""
        if not full_url:
            continue
        candidates.append({
            "title": title,
            "url": full_url,
            "company": field("company"),
            "location": field("location"),
            "date_posted": field("date_posted"),
            "salary": field("salary"),
        })
    return candidates


def scrape_source(source: dict, sj_mod) -> list[dict]:
    name = source["name"]
    url = source["url"]
    engine = source.get("engine", "stdlib")
    print(f"🌍 {name} ({url}) [engine={engine}]")

    if engine == "scrapling":
        page = _fetch_via_scrapling(url)
        if page is None:
            return []
        raw_candidates = extract_jobs_scrapling(page, source.get("selectors"), url)
    else:
        page_html = fetch_html(url)
        if page_html is None:
            return []
        raw_candidates = extract_jobs_heuristic(page_html, url)
    print(f"    {len(raw_candidates)} candidate link(s) found")

    already_scoped = source.get("country") not in ("Global", "EU-wide")
    target_terms = [str(t).lower() for t in sj_mod._cfg("location_filter.terms", [])]

    jobs = []
    for raw in raw_candidates:
        title = raw["title"]
        if not sj_mod.is_mle_role(title):
            continue
        # The scrapling path (precise selectors) or heading-extraction in the
        # heuristic path (see extract_jobs_heuristic) may already have a real
        # company and/or location; only guess whichever one is still missing
        # -- not one-or-the-other as a pair. (A source can supply a clean
        # company via a nested heading, e.g. Proptech Jobs, while its location
        # still needs the container-text guess -- that's a company-only
        # heading, not both fields, so this must not skip the location guess.)
        company = raw.get("company") or ""
        location = raw.get("location") or ""
        if not company or not location:
            guessed_company, guessed_location = _guess_company_and_location(raw, target_terms)
            company = company or guessed_company
            location = location or guessed_location
        if not already_scoped and target_terms and not sj_mod.is_target_location(location):
            continue
        jobs.append({
            "title": title,
            "company": company or name,  # fall back to the board name so nothing is blank
            "location": location or source.get("country", ""),
            "url": raw["url"],
            "date_posted": raw.get("date_posted", ""),
            "description": "",
            "salary": raw.get("salary", ""),
            "job_type": "",
            "is_remote": None,
            "work_arrangement": sj_mod.classify_work_arrangement(location),
            "ats": name,
        })
    print(f"    ✅ {len(jobs)} role(s) matched keywords" + (" + location" if not already_scoped else ""))
    return jobs


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    only_source = None
    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        if idx + 1 < len(sys.argv):
            only_source = sys.argv[idx + 1]

    sj_mod = _import_scrape_jobs()
    sources = _load_config()

    all_jobs: list[dict] = []
    seen_urls: set[str] = set()

    for i, source in enumerate(sources):
        if only_source and source["id"] != only_source:
            continue
        if not only_source and not source.get("enabled", False):
            continue
        if i > 0:
            time.sleep(REQUEST_DELAY)
        try:
            jobs = scrape_source(source, sj_mod)
        except Exception as e:  # noqa: BLE001 -- one bad source must never kill the run
            print(f"    ⛔ {source['name']} errored, skipping this run: {e}")
            continue
        for job in jobs:
            ident = sj_mod._job_identity(job["url"])
            if ident and ident in seen_urls:
                continue
            if ident:
                seen_urls.add(ident)
            all_jobs.append(job)

    if only_source:
        sources_run = len([s for s in sources if s["id"] == only_source])
    else:
        sources_run = len([s for s in sources if s.get("enabled", False)])
    print(f"\n📋 Total: {len(all_jobs)} role(s) across {sources_run} source(s)")

    if dry_run:
        print("\n--dry-run: not saving or touching all_jobs.json. Sample titles:")
        for job in all_jobs[:10]:
            print(f"  - [{job['ats']}] {job['title']} — {job['company']} ({job['location']}) {job['url']}")
        return 0

    profile_label = getattr(sj_mod, "PROFILE_LABEL", "Target")
    profile_subtitle = getattr(sj_mod, "PROFILE_SUBTITLE", "")
    sj_mod.save_jobs_output(
        all_jobs,
        basename="additional_sources_jobs",
        title=f"🌍 Additional Job Sources — {profile_label} Roles",
        subtitle=f"{profile_subtitle} · niche UK/IE/NL/DE/EU boards · daily" if profile_subtitle
                 else "niche UK/IE/NL/DE/EU boards · daily",
        accent="#7c3aed",
        empty_message="No new roles from the additional job-source boards since the last run.",
        window_label="current postings across enabled additional sources",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
