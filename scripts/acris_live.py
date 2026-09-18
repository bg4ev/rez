#!/usr/bin/env python3
"""
acris_live.py

Pulls recorded documents from the LIVE ACRIS site, which is current to yesterday,
unlike NYC Open Data, which publishes monthly and runs two to six weeks behind.

Two steps per run:
  1. Document-type search  -> document ids + index row (POST, needs a token)
  2. Document Detail page  -> party names, mailing addresses, parcel, property
                              type, street address (plain GET, no token)

Writes acris_live.csv and acris_live.json. No OCR here; ctor_read.py adds the
order type afterward from the document images.

Usage:
  python3 acris_live.py
  python3 acris_live.py --days 30
  python3 acris_live.py --start 2026-09-01 --end 2026-09-17
  python3 acris_live.py --doctype CTOR --borough 0 --out acris_live
  python3 acris_live.py --no-detail          # index rows only, much faster
"""

import argparse
import csv
import datetime
import html as htmlmod
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UA = "Mozilla/5.0"
BASE = "https://a836-acris.nyc.gov/DS/DocumentSearch/"
FORM_URL = BASE + "DocumentType"
RESULT_URL = BASE + "DocumentTypeResult"
DETAIL_URL = BASE + "DocumentDetail?doc_id={}"
IMAGE_URL = BASE + "DocumentImageView?doc_id={}"

MAX_ROWS = 50          # server ceiling; larger values silently fall back to 10
MAX_PAGES = 40         # safety stop
TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')
DOCID_RE = re.compile(r'go_detail\("(\d{16})"\)')

# result row cell positions, verified against the live page
COL = {"borough": 1, "block": 2, "reel": 3, "crfn": 4, "lot": 5, "partial": 6,
       "doc_date": 7, "recorded": 8, "pages": 9, "party1": 10, "party2": 11,
       "doc_amount": 15}


def clean(fragment):
    """Strip tags and entities from a chunk of HTML, collapse whitespace."""
    txt = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", htmlmod.unescape(txt).replace("\xa0", " ")).strip()


def curl(args, jar):
    try:
        r = subprocess.run(["curl", "-s", "-S", "--max-time", "90",
                            "-A", UA, "-b", jar, "-c", jar] + args,
                           capture_output=True, text=True, timeout=120)
        return r.stdout or ""
    except Exception:
        return ""


def table_after(page, marker_pos):
    """Return the rows of the first <table> following a position in the page.

    The detail page puts column headers and data in SEPARATE tables, with the
    data table inside a scrolling div whose cells contain nested divs. Anchoring
    on the marker and taking the next table avoids both traps.
    """
    start = page.find("<table", marker_pos)
    if start < 0:
        return []
    end = page.find("</table>", start)
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page[start:end], re.S):
        cells = [clean(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if any(cells):
            rows.append(cells)
    return rows


def at(cells, i):
    return cells[i] if i < len(cells) else ""


def search_page(page_no, doctype, start, end, borough, jar):
    """One page of document-type results. Returns [(doc_id, row cells), ...]."""
    token_match = TOKEN_RE.search(curl([FORM_URL], jar))
    if not token_match:
        return None
    fields = [
        f"hid_doctype={doctype}", "hid_doctype_name=", "hid_selectdate=DR",
        f"hid_datefromm={start.month}", f"hid_datefromd={start.day}",
        f"hid_datefromy={start.year}", f"hid_datetom={end.month}",
        f"hid_datetod={end.day}", f"hid_datetoy={end.year}",
        f"hid_borough={borough}", "hid_borough_name=",
        f"hid_max_rows={MAX_ROWS}", f"hid_page={page_no}",
        "hid_SearchType=DOCTYPE", "hid_ISIntranet=N", "hid_sort=",
        "__RequestVerificationToken=" + token_match.group(1),
    ]
    url = f"{RESULT_URL}?page={page_no}&max_rows={MAX_ROWS}"
    page = curl(["-X", "POST", "--data", "&".join(fields), url], jar)

    out = []
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", page, re.S):
        m = DOCID_RE.search(tr)
        if not m:
            continue
        cells = [clean(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        out.append((m.group(1), cells))
    return out


def fetch_detail(doc_id, jar, tries=3):
    """Party names, mailing addresses and parcel data. No token needed.

    The page occasionally comes back truncated mid-session. Retry rather than
    dropping the record, and say so instead of returning a silent blank."""
    page = ""
    for n in range(tries):
        page = curl([DETAIL_URL.format(doc_id)], jar)
        if len(page) > 5000 and "ABPOSITION" in page:
            break
        time.sleep(1.5 * (n + 1))
    else:
        return {"detail_error": f"short page after {tries} tries"}, 0
    marks = [m.start() for m in re.finditer(r'<div id="ABPOSITION"', page)]
    blocks = [table_after(page, m) for m in marks]
    while len(blocks) < 5:
        blocks.append([])

    def party(rows, prefix):
        r = rows[0] if rows else []
        return {f"{prefix}_name": at(r, 0), f"{prefix}_address": at(r, 1),
                f"{prefix}_address2": at(r, 2), f"{prefix}_city": at(r, 3),
                f"{prefix}_state": at(r, 4), f"{prefix}_zip": at(r, 5)}

    d = {}
    d.update(party(blocks[0], "p1"))
    d.update(party(blocks[1], "p2"))
    d["p3_name"] = at(blocks[2][0], 0) if blocks[2] else ""

    parcels = blocks[3]
    p = parcels[0] if parcels else []
    d.update({"parcel_borough": at(p, 0), "parcel_block": at(p, 1),
              "parcel_lot": at(p, 2), "property_type": at(p, 4),
              "property_address": at(p, 8), "parcel_count": len(parcels)})
    return d, len(parcels)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start", help="YYYY-MM-DD, overrides --days")
    ap.add_argument("--end", help="YYYY-MM-DD, defaults to today")
    ap.add_argument("--doctype", default="CTOR")
    ap.add_argument("--borough", default="0", help="0 = all boroughs")
    ap.add_argument("--out", default="acris_live", help="output file stem")
    ap.add_argument("--sleep", type=float, default=0.6)
    ap.add_argument("--no-detail", action="store_true",
                    help="skip the per-document detail fetch")
    ap.add_argument("--limit", type=int, help="cap documents, for testing")
    args = ap.parse_args()

    end = (datetime.date.fromisoformat(args.end) if args.end
           else datetime.date.today())
    start = (datetime.date.fromisoformat(args.start) if args.start
             else end - datetime.timedelta(days=args.days))
    if start > end:
        sys.exit("start date is after end date")
    span = (end - start).days
    if span > 31:
        sys.exit(f"range is {span} days; ACRIS caps document-type search at 31 "
                 f"and returns an empty result above it. Run it in chunks:\n"
                 f"  python3 acris_live.py --start {start} "
                 f"--end {start + datetime.timedelta(days=31)} --out chunk1")

    jar_dir = tempfile.TemporaryDirectory()
    jar = str(Path(jar_dir.name) / "acris.jar")

    print(f"{args.doctype}  {start} to {end}  borough {args.borough}")
    found, seen = [], set()
    for page_no in range(1, MAX_PAGES + 1):
        rows = search_page(page_no, args.doctype, start, end, args.borough, jar)
        if rows is None:
            sys.exit("could not get a request token; the search form may have changed")
        fresh = [(d, c) for d, c in rows if d not in seen]
        for d, _ in fresh:
            seen.add(d)
        found.extend(fresh)
        print(f"  page {page_no}: {len(rows)} rows ({len(found)} total)")
        if len(rows) < MAX_ROWS:
            break
        time.sleep(args.sleep)

    if not found:
        sys.exit("no documents in that window")
    if args.limit:
        found = found[:args.limit]

    records = []
    for n, (doc_id, cells) in enumerate(found, 1):
        rec = {
            "document_id": doc_id,
            "crfn": at(cells, COL["crfn"]),
            "borough": at(cells, COL["borough"]),
            "block": at(cells, COL["block"]),
            "lot": at(cells, COL["lot"]),
            "doc_date": at(cells, COL["doc_date"]),
            "recorded": at(cells, COL["recorded"]),
            "pages": at(cells, COL["pages"]),
            "party1": at(cells, COL["party1"]),
            "party2": at(cells, COL["party2"]),
        }
        if not args.no_detail:
            detail, _ = fetch_detail(doc_id, jar)
            rec.update(detail)
            time.sleep(args.sleep)
            if n % 10 == 0 or n == len(found):
                print(f"  detail {n}/{len(found)}")
        rec["acris_link"] = IMAGE_URL.format(doc_id)
        rec["detail_link"] = DETAIL_URL.format(doc_id)
        records.append(rec)

    fields = list(records[0].keys())
    for r in records:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(f"{args.out}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(records)
    with open(f"{args.out}.json", "w", encoding="utf-8") as fh:
        json.dump({"generated": datetime.datetime.now().isoformat(timespec="seconds"),
                   "doc_type": args.doctype,
                   "range": {"start": start.isoformat(), "end": end.isoformat()},
                   "borough": args.borough,
                   "count": len(records),
                   "records": records}, fh, indent=1)

    addr = sum(1 for r in records if r.get("property_address"))
    own = sum(1 for r in records if r.get("p2_address"))
    err = sum(1 for r in records if r.get("detail_error"))
    print(f"\n{len(records)} documents")
    if not args.no_detail:
        print(f"property address: {addr}   party 2 mailing address: {own}")
        if err:
            print(f"detail fetch failed after retries: {err}")
    print(f"wrote {args.out}.csv and {args.out}.json")


if __name__ == "__main__":
    main()
