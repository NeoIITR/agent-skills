#!/usr/bin/env python3
"""Momentum screen over TrustMRR startups.

Finds young startups in a revenue band whose revenue has grown steadily for
several months, ranks them, and summarises by category.

Standard library only. Usage:

    export TRUSTMRR_API_KEY=tmrr_...
    python momentum_screen.py --inspect          # show what the API returns
    python momentum_screen.py                    # run the screen
    python momentum_screen.py --from-file raw.json   # rerun offline on a saved dump

The TrustMRR response schema was not verifiable when this was written, so field
names are looked up from candidate lists in FIELDS below. Run --inspect first
and adjust FIELDS if anything comes back empty.
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

BASE_URL = "https://trustmrr.com/api/v1"
REQUEST_INTERVAL = 3.1  # 20 requests/minute limit

# First matching key wins. Dotted paths reach into nested objects.
FIELDS = {
    "slug": ["slug", "id"],
    "name": ["name", "title"],
    "category": ["category", "category.name", "categories", "niche", "industry"],
    "url": ["website", "url", "domain"],
    "launched": ["foundedDate", "founded_at", "launchedAt", "launched_at", "createdAt", "created_at", "foundedAt"],
    "mrr": ["mrr", "revenue.mrr", "currentMrr", "revenue.last30Days", "last30DaysRevenue", "revenue_last_30_days"],
    "history": ["revenueHistory", "revenue_history", "monthlyRevenue", "monthly_revenue", "revenue.monthly", "history"],
}
MONEY_IN_CENTS = True  # reported by third-party docs; set False if values look 100x too big


# ---------------------------------------------------------------- field access

def pick(record, field):
    for path in FIELDS[field]:
        value = record
        for part in path.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                value = None
                break
        if value not in (None, "", []):
            return value
    return None


def to_dollars(value):
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount / 100 if MONEY_IN_CENTS else amount


def parse_date(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):  # epoch seconds or ms
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc).date()
    text = str(value)
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(text[:len(fmt) + 2], fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def category_of(record):
    value = pick(record, "category")
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("name") or value.get("slug")
    return str(value) if value else "Uncategorised"


def monthly_series(record):
    """Return [(YYYY-MM, dollars)] oldest first, from whatever shape history has."""
    raw = pick(record, "history")
    points = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = []
        for row in raw:
            if isinstance(row, dict):
                key = row.get("month") or row.get("date") or row.get("period") or row.get("timestamp")
                val = row.get("revenue", row.get("mrr", row.get("amount", row.get("value"))))
                items.append((key, val))
    else:
        items = []
    for key, val in items:
        day = parse_date(key)
        dollars = to_dollars(val)
        if day and dollars is not None:
            month = day.strftime("%Y-%m")
            points[month] = points.get(month, 0) + dollars  # daily data rolls up to months
    return sorted(points.items())


def months_between(start, end):
    return (end.year - start.year) * 12 + (end.month - start.month)


# ---------------------------------------------------------------- API

def api_get(path, api_key, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "momentum-screen/1.0",
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                time.sleep(REQUEST_INTERVAL)
                return json.load(response)
        except urllib.error.HTTPError as err:
            if err.code == 429 or err.code >= 500:
                wait = int(err.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
                print(f"  HTTP {err.code}, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            body = err.read().decode(errors="replace")[:300]
            sys.exit(f"HTTP {err.code} for {url}: {body}")
    sys.exit(f"Gave up on {url} after repeated errors")


def unwrap_list(payload):
    if isinstance(payload, list):
        return payload, None
    for key in ("data", "startups", "results", "items"):
        if isinstance(payload.get(key), list):
            meta = payload.get("pagination") or payload.get("meta") or payload
            return payload[key], meta
    return [], None


def fetch_all(api_key, max_pages):
    startups, page = [], 1
    while page <= max_pages:
        payload = api_get("/startups", api_key, {"page": page, "limit": 100})
        rows, meta = unwrap_list(payload)
        if not rows:
            break
        startups.extend(rows)
        print(f"  page {page}: {len(rows)} startups (total {len(startups)})", file=sys.stderr)
        has_more = None
        if isinstance(meta, dict):
            has_more = meta.get("hasMore", meta.get("has_more"))
            total_pages = meta.get("totalPages") or meta.get("total_pages")
            if total_pages:
                has_more = page < int(total_pages)
        if has_more is False or (has_more is None and len(rows) < 100):
            break
        page += 1
    return startups


def fetch_detail(api_key, record):
    slug = pick(record, "slug")
    if not slug:
        return record
    payload = api_get(f"/startups/{urllib.parse.quote(str(slug))}", api_key)
    detail = payload.get("data", payload) if isinstance(payload, dict) else {}
    return {**record, **detail}


# ---------------------------------------------------------------- scoring

def score(series, months, dip_tolerance, spike_share):
    """Score the last `months` monthly values. Returns None if too little data."""
    # The current month is partial, so leave it out.
    complete = [v for month, v in series if month != date.today().strftime("%Y-%m")]
    values = complete[-(months + 1):]
    if len(values) < 3:
        return None
    growth = []
    for prev, cur in zip(values, values[1:]):
        growth.append((cur - prev) / prev if prev > 0 else (1.0 if cur > 0 else 0.0))

    streak = 0
    for g in reversed(growth):
        if g >= -dip_tolerance:
            streak += 1
        else:
            break
    total_gain = values[-1] - values[0]
    biggest_jump = max((cur - prev for prev, cur in zip(values, values[1:])), default=0)
    spiky = total_gain > 0 and biggest_jump / total_gain > spike_share and len(growth) >= 3
    positive_share = sum(g > 0 for g in growth) / len(growth)
    median_growth = statistics.median(growth)
    compound = (values[-1] / values[0]) ** (1 / len(growth)) - 1 if values[0] > 0 else None
    # Consistency first, size of growth second; spikes are penalised, not dropped.
    momentum = streak * 10 + positive_share * 20 + min(median_growth, 1.0) * 50 - (25 if spiky else 0)
    return {
        "months_scored": len(growth),
        "growth_streak": streak,
        "positive_month_share": round(positive_share, 2),
        "median_mom_growth": round(median_growth, 3),
        "compound_mom_growth": round(compound, 3) if compound is not None else "",
        "spiky": spiky,
        "momentum_score": round(momentum, 1),
        "series": " > ".join(f"{v:,.0f}" for v in values),
    }


# ---------------------------------------------------------------- snapshots

def save_snapshot(startups, folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{date.today():%Y-%m-%d}.json"
    path.write_text(json.dumps(startups))
    return path


def history_from_snapshots(folder):
    """Build {slug: [(YYYY-MM, mrr)]} from saved snapshots, last snapshot per month."""
    by_slug = defaultdict(dict)
    for path in sorted(folder.glob("*.json")):
        month = path.stem[:7]
        for record in json.loads(path.read_text()):
            slug, mrr = pick(record, "slug"), to_dollars(pick(record, "mrr"))
            if slug and mrr is not None:
                by_slug[slug][month] = mrr
    return {slug: sorted(months.items()) for slug, months in by_slug.items()}


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--min-mrr", type=float, default=500)
    parser.add_argument("--max-mrr", type=float, default=5000)
    parser.add_argument("--min-age-months", type=int, default=0)
    parser.add_argument("--max-age-months", type=int, default=12)
    parser.add_argument("--months", type=int, default=4, help="months of history to score")
    parser.add_argument("--min-streak", type=int, default=2, help="consecutive non-declining months required")
    parser.add_argument("--dip-tolerance", type=float, default=0.05, help="a month-over-month drop this small still counts as holding")
    parser.add_argument("--spike-share", type=float, default=0.6, help="flag as spiky if one month holds more than this share of total gain")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--no-details", action="store_true", help="skip per-startup detail calls")
    parser.add_argument("--from-file", type=Path, help="use a saved raw dump instead of the API")
    parser.add_argument("--inspect", action="store_true", help="print one raw record and its detected fields, then exit")
    parser.add_argument("--out", type=Path, default=Path("output"))
    args = parser.parse_args()

    api_key = os.environ.get("TRUSTMRR_API_KEY")
    if args.from_file:
        startups = json.loads(args.from_file.read_text())
    else:
        if not api_key:
            sys.exit("Set TRUSTMRR_API_KEY (from your TrustMRR developer dashboard).")
        if args.inspect:
            rows, _ = unwrap_list(api_get("/startups", api_key, {"page": 1, "limit": 1}))
            if not rows:
                sys.exit("No startups returned.")
            rows[0] = fetch_detail(api_key, rows[0])
            startups = rows
        else:
            print("Fetching startup list...", file=sys.stderr)
            startups = fetch_all(api_key, args.max_pages)

    if args.inspect:
        record = startups[0]
        print(json.dumps(record, indent=2)[:4000])
        print("\nDetected fields:")
        for field in FIELDS:
            print(f"  {field:9} -> {pick(record, field)!r}"[:160])
        print(f"  series    -> {monthly_series(record)[:6]}")
        return

    args.out.mkdir(parents=True, exist_ok=True)
    if not args.from_file:
        (args.out / "raw_startups.json").write_text(json.dumps(startups))
        print(f"Snapshot saved: {save_snapshot(startups, args.out / 'snapshots')}", file=sys.stderr)

    today = date.today()
    candidates = []
    for record in startups:
        mrr = to_dollars(pick(record, "mrr"))
        launched = parse_date(pick(record, "launched"))
        if mrr is None or not (args.min_mrr <= mrr <= args.max_mrr):
            continue
        if launched is None:
            continue
        age = months_between(launched, today)
        if not (args.min_age_months <= age <= args.max_age_months):
            continue
        candidates.append((record, mrr, launched, age))
    print(f"{len(startups)} startups, {len(candidates)} in revenue band and age window", file=sys.stderr)

    snapshot_history = history_from_snapshots(args.out / "snapshots")
    rows = []
    for i, (record, mrr, launched, age) in enumerate(candidates, 1):
        if not args.from_file and not args.no_details and not monthly_series(record):
            print(f"  detail {i}/{len(candidates)}: {pick(record, 'slug')}", file=sys.stderr)
            record = fetch_detail(api_key, record)
        series = monthly_series(record) or snapshot_history.get(pick(record, "slug"), [])
        result = score(series, args.months, args.dip_tolerance, args.spike_share)
        if result is None or result["growth_streak"] < args.min_streak:
            continue
        rows.append({
            "name": pick(record, "name"),
            "category": category_of(record),
            "mrr": round(mrr),
            "launched": launched.isoformat(),
            "age_months": age,
            **result,
            "url": pick(record, "url") or "",
            "slug": pick(record, "slug"),
        })

    rows.sort(key=lambda r: r["momentum_score"], reverse=True)
    if not rows:
        print("No startups passed the growth filter. If none had revenue history, run this monthly: "
              "snapshots in output/snapshots build the history over time.", file=sys.stderr)
        return

    ranked_path = args.out / "ranked_startups.csv"
    with ranked_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    groups = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)
    summary = []
    for category, members in groups.items():
        steady = [r for r in members if not r["spiky"]]
        summary.append({
            "category": category,
            "startups": len(members),
            "steady_growers": len(steady),
            "median_mrr": round(statistics.median(r["mrr"] for r in members)),
            "median_mom_growth": round(statistics.median(r["median_mom_growth"] for r in members), 3),
            "avg_momentum": round(statistics.mean(r["momentum_score"] for r in members), 1),
            "top_examples": "; ".join(r["name"] for r in members[:3]),
        })
    summary.sort(key=lambda s: (s["steady_growers"], s["avg_momentum"]), reverse=True)
    summary_path = args.out / "category_summary.csv"
    with summary_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)

    print(f"\nTop startups ({len(rows)} passed):")
    for r in rows[:15]:
        flag = " (spiky)" if r["spiky"] else ""
        print(f"  {r['momentum_score']:6.1f}  {r['name'][:30]:30} {r['category'][:20]:20} "
              f"${r['mrr']:>6,}  streak {r['growth_streak']}  median MoM {r['median_mom_growth']:+.0%}{flag}")
    print("\nCategories:")
    for s in summary[:15]:
        print(f"  {s['category'][:28]:28} {s['steady_growers']:>3} steady / {s['startups']:>3}  "
              f"median MoM {s['median_mom_growth']:+.0%}  e.g. {s['top_examples'][:60]}")
    print(f"\nWrote {ranked_path} and {summary_path}")


if __name__ == "__main__":
    main()
