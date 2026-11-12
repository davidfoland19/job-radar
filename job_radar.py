#!/usr/bin/env python3
"""
Job Radar - pulls open roles from Greenhouse, Lever and Ashby job boards
for a list of target companies, filters for roles you care about, and
builds a local HTML dashboard + CSV.

No API keys, no installs. Python 3.9+ standard library only.

Run:   python3 job_radar.py
Opens: dashboard.html (and writes jobs.csv)

Edit companies.json to change targets, keywords and locations.
"""

import csv
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "companies.json")
SEEN_PATH = os.path.join(HERE, "seen_jobs.json")
CSV_PATH = os.path.join(HERE, "jobs.csv")
HTML_PATH = os.path.join(HERE, "dashboard.html")

UA = {"User-Agent": "job-radar/1.0 (personal job search)"}


# ---------- fetching ----------

def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def s(value):
    """Always return a string, even if the API sent null or a number."""
    return "" if value is None else str(value)


def to_iso(value):
    """Normalize ISO strings or epoch-ms ints to YYYY-MM-DD."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        return str(value)[:10]
    except Exception:
        return ""


def from_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not data or "jobs" not in data:
        return None
    return [{
        "id": f"gh-{j.get('id')}",
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "department": ", ".join(d.get("name", "") for d in j.get("departments", []) or []),
        "posted": to_iso(j.get("first_published") or j.get("updated_at")),
        "url": j.get("absolute_url", ""),
        "source": "Greenhouse",
    } for j in data["jobs"]]


def from_lever(slug):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return None
    out = []
    for j in data:
        cat = j.get("categories") or {}
        out.append({
            "id": f"lv-{j.get('id')}",
            "title": j.get("text", ""),
            "location": cat.get("location", "") or ", ".join(cat.get("allLocations", []) or []),
            "department": cat.get("team", "") or cat.get("department", ""),
            "posted": to_iso(j.get("createdAt")),
            "url": j.get("hostedUrl", ""),
            "source": "Lever",
        })
    return out


def from_ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not data or "jobs" not in data:
        return None
    out = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        loc = s(j.get("location"))
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        out.append({
            "id": f"ab-{j.get('id')}",
            "title": j.get("title", ""),
            "location": loc,
            "department": j.get("department", "") or j.get("team", ""),
            "posted": to_iso(j.get("publishedAt")),
            "url": j.get("jobUrl", ""),
            "source": "Ashby",
        })
    return out


BOARDS = [from_greenhouse, from_lever, from_ashby]


def from_jsearch(query, date_posted, api_key):
    """Keyword search across Google for Jobs (LinkedIn, Indeed, Glassdoor, career pages)."""
    url = ("https://jsearch.p.rapidapi.com/search?"
           + urllib.parse.urlencode({"query": query, "page": 1, "num_pages": 1,
                                     "country": "us", "date_posted": date_posted}))
    req = urllib.request.Request(url, headers={**UA, "X-RapidAPI-Key": api_key,
                                               "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  ✗ JSearch '{query}': {e}")
        return []
    out = []
    for j in data.get("data", []) or []:
        loc = ", ".join(x for x in [j.get("job_city"), j.get("job_state")] if x)
        if j.get("job_is_remote"):
            loc = f"{loc} (Remote)".strip() if loc else "Remote"
        out.append({
            "id": f"js-{j.get('job_id')}",
            "company": j.get("employer_name", ""),
            "title": j.get("job_title", ""),
            "location": loc,
            "department": "",
            "posted": to_iso(j.get("job_posted_at_datetime_utc")),
            "url": j.get("job_apply_link", ""),
            "source": f"Search · {j.get('job_publisher', 'web')}",
            "status": "",
        })
    print(f"  ✓ JSearch '{query}'  ({len(out)} results)")
    return out


def fetch_company(company):
    """Try every slug on every ATS until one returns jobs."""
    for slug in company["slugs"]:
        for board in BOARDS:
            try:
                jobs = board(slug)
            except Exception as e:
                print(f"  ! {company['name']} ({board.__name__[5:]}:{slug}) skipped: {e}")
                jobs = None
            if jobs is not None:
                for j in jobs:
                    j["company"] = company["name"]
                    j["status"] = company.get("status", "")
                return company["name"], jobs, f"{jobs[0]['source'] if jobs else board.__name__[5:].title()}:{slug}"
    return company["name"], [], None


# ---------- filtering & scoring ----------

def has_any(text, words):
    """True if any word/phrase appears as a whole word (so 'uk' won't match 'Milwaukee')."""
    return any(re.search(r"(?<![a-z])" + re.escape(w.lower()) + r"(?![a-z])", text) for w in words)


US_WORDS = ["united states", "usa", "us", "u.s.", "u.s", "america", "americas", "north america", "us-remote", "us remote"]
US_STATES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|"
             "idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|"
             "minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
             "new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|"
             "south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|"
             "wisconsin|wyoming")
STATE_ABBR = re.compile(r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
                        r"MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b")


def looks_us(raw):
    low = raw.lower()
    return (has_any(low, US_WORDS) or bool(STATE_ABBR.search(raw))
            or bool(re.search(r"(?<![a-z])(" + US_STATES + r")(?![a-z])", low)))


def location_ok(raw, cfg, title=""):
    """Keep: your cities, or remote jobs that are clearly US (or say only 'Remote')."""
    loc = raw.lower()
    if not loc:
        return False
    if has_any(loc + " " + title.lower(), cfg["exclude_locations"]):
        return False
    if has_any(loc, cfg["cities"]):
        return True
    if not has_any(loc, cfg["remote_words"]):
        return False
    if looks_us(raw):
        return True
    # Plain "Remote" / "Remote (Hybrid)" with no other place named
    leftover = re.sub(r"remote|hybrid|anywhere|work from home|wfh|fully|[^a-z]", "", loc)
    return leftover == ""


def score(job, cfg):
    t = job["title"].lower()
    raw_loc = job["location"]
    loc = raw_loc.lower()
    if any(x.lower() in t for x in cfg["exclude_title"]):
        return 0
    if not any(k.lower() in t for k in cfg["title_keywords"]):
        return 0
    if not location_ok(raw_loc, cfg, job["title"]):
        return 0
    s = 50
    if any(p.lower() in t for p in cfg["preferred_title"]):
        s += 25
    if has_any(loc, cfg["top_locations"]):
        s += 25
    return s


# ---------- output ----------

def write_csv(jobs):
    cols = ["new", "score", "company", "title", "location", "department", "posted", "source", "status", "url"]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for j in jobs:
            w.writerow({**j, "new": "NEW" if j["new"] else ""})


def write_html(jobs, report):
    now = datetime.now().strftime("%b %d, %Y %I:%M %p")
    new_count = sum(1 for j in jobs if j["new"])
    companies_hit = len({j["company"] for j in jobs})
    rows = []
    for j in jobs:
        e = {k: html.escape(str(v)) for k, v in j.items()}
        badge = "<span class=new>NEW</span>" if j["new"] else ""
        tag = f"<div class=tag>{e['status']}</div>" if j["status"] else ""
        rows.append(
            f'<tr data-new="{int(j["new"])}">'
            f'<td>{badge}</td>'
            f'<td class=num>{j["score"]}</td>'
            f'<td><b>{e["company"]}</b>{tag}</td>'
            f'<td><a href="{e["url"]}" target="_blank" rel="noopener">{e["title"]}</a></td>'
            f'<td>{e["location"]}</td><td>{e["posted"]}</td><td class=muted>{e["source"]}</td></tr>'
        )
    found = "".join(f"<li><b>{html.escape(n)}</b> — {html.escape(s)}</li>" for n, s in report if s)
    missing = ", ".join(html.escape(n) for n, s in report if not s) or "none"

    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Job Radar</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#fafaf9;--card:#fff;--ink:#1c1917;--muted:#78716c;--line:#e7e5e4;--accent:#0f766e}}
*{{box-sizing:border-box}}body{{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}}
main{{max-width:1150px;margin:0 auto;padding:28px 20px}}h1{{margin:0;font-size:22px}}.sub{{color:var(--muted);margin:4px 0 20px}}
.kpis{{display:flex;gap:12px;margin-bottom:18px;flex-wrap:wrap}}.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:140px}}
.kpi b{{display:block;font-size:24px}}.kpi span{{color:var(--muted);font-size:12px}}
.bar{{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}}input,select{{padding:8px 10px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff}}input{{flex:1;min-width:220px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}}th{{background:#f5f5f4;font-size:12px;color:var(--muted);cursor:pointer;user-select:none}}
a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}.num{{font-variant-numeric:tabular-nums}}.muted{{color:var(--muted)}}
.new{{background:#ccfbf1;color:#115e59;font-size:11px;font-weight:600;padding:2px 6px;border-radius:4px}}.tag{{font-size:11px;color:var(--muted)}}
details{{margin-top:18px;color:var(--muted)}}
</style></head><body><main>
<h1>Job Radar</h1><div class=sub>Updated {now} · Greenhouse, Lever, Ashby + open-web search</div>
<div class=kpis><div class=kpi><b>{len(jobs)}</b><span>matching roles</span></div>
<div class=kpi><b>{new_count}</b><span>new since last run</span></div>
<div class=kpi><b>{companies_hit}</b><span>companies hiring</span></div></div>
<div class=bar><input id=q placeholder="Filter by company, title, location…">
<select id=f><option value=all>All roles</option><option value=new>New only</option></select></div>
<table id=t><thead><tr><th></th><th>Score</th><th>Company</th><th>Role</th><th>Location</th><th>Posted</th><th>Source</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=7 class=muted>No matching roles. Loosen title_keywords in companies.json.</td></tr>'}</tbody></table>
<details><summary>Board coverage</summary><ul>{found}</ul><p>Not found on any board (fix the slug in companies.json): {missing}</p></details>
</main><script>
const q=document.getElementById('q'),f=document.getElementById('f'),rows=[...document.querySelectorAll('#t tbody tr')];
function apply(){{const s=q.value.toLowerCase();rows.forEach(r=>{{const ok=r.textContent.toLowerCase().includes(s)&&(f.value==='all'||r.dataset.new==='1');r.style.display=ok?'':'none'}})}}
q.oninput=apply;f.onchange=apply;
document.querySelectorAll('#t th').forEach((th,i)=>th.onclick=()=>{{const tb=document.querySelector('#t tbody');const dir=th.dataset.d=th.dataset.d==='1'?'-1':'1';
[...tb.rows].sort((a,b)=>{{const x=a.cells[i].innerText,y=b.cells[i].innerText;const nx=+x,ny=+y;return (isNaN(nx)||isNaN(ny)?x.localeCompare(y):nx-ny)*dir}}).forEach(r=>tb.appendChild(r))}});
</script></body></html>"""
    with open(HTML_PATH, "w", encoding="utf-8") as fh:
        fh.write(page)


# ---------- main ----------

def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    seen = set()
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH, encoding="utf-8") as f:
            seen = set(json.load(f))
    first_run = not seen

    print("Job Radar v4 (US-only remote)")
    print(f"Checking {len(cfg['companies'])} companies…")
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(fetch_company, cfg["companies"]))

    all_jobs, report = [], []
    for name, jobs, where in results:
        report.append((name, where))
        print(f"  {'✓' if where else '✗'} {name:<22} {where or 'not found on Greenhouse/Lever/Ashby'}  ({len(jobs)} open roles)")
        all_jobs.extend(jobs)

    # Open-web keyword search (any company). Needs a free RapidAPI key for JSearch.
    key = os.environ.get("JSEARCH_API_KEY") or cfg.get("jsearch_api_key", "")
    key_file = os.path.join(HERE, "jsearch_key.txt")
    if (not key or key.startswith("PASTE")) and os.path.exists(key_file):
        key = open(key_file, encoding="utf-8").read().strip()
    if key and not key.startswith("PASTE"):
        print(f"\nSearching the open web ({len(cfg.get('searches', []))} searches)…")
        status_by_co = {c["name"].lower(): c.get("status", "") for c in cfg["companies"]}
        for q in cfg.get("searches", []):
            for j in from_jsearch(q, cfg.get("search_date_posted", "3days"), key):
                j["status"] = status_by_co.get(j["company"].lower(), "")
                all_jobs.append(j)
    else:
        print("\n(Open-web search skipped: add your JSearch key to companies.json)")

    for j in all_jobs:
        for k in ("id", "company", "title", "location", "department", "posted", "url", "source", "status"):
            j[k] = s(j.get(k))

    # De-dupe: same company + title seen from two sources counts once
    deduped, keys = [], set()
    for j in all_jobs:
        k = (j["company"].strip().lower(), j["title"].strip().lower())
        if k not in keys:
            keys.add(k)
            deduped.append(j)
    all_jobs = deduped

    matches = []
    for j in all_jobs:
        sc = score(j, cfg)
        if sc:
            j["score"] = sc
            j["new"] = (not first_run) and j["id"] not in seen
            matches.append(j)
    matches.sort(key=lambda j: (not j["new"], -j["score"], j["company"]))

    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen | {j["id"] for j in all_jobs}), f)

    write_csv(matches)
    write_html(matches, report)
    print(f"\n{len(matches)} matching roles ({sum(j['new'] for j in matches)} new). Dashboard: {HTML_PATH}")
    if "--no-open" not in sys.argv:
        webbrowser.open("file://" + HTML_PATH)


if __name__ == "__main__":
    main()
