#!/usr/bin/env python3
"""
ctor_leads.py

Takes the document ids produced by acris_live.py, reads each document's page
images, and adds what the court actually did to the contact data already in
the record.

Two targets, in Elana's priority order:
  in_rem  -- 11 U.S.C. 362(d)(4) stay relief. Owner is barred from filing a
             further bankruptcy against the property for two years.
  jfs     -- judgment of foreclosure and sale.

Everything else is kept and titled, never dropped. The title is whatever the
document calls itself, quoted verbatim, so a human can read it and decide.

Usage:
  python3 ctor_leads.py
  python3 ctor_leads.py --json acris_live.json --out leads
  python3 ctor_leads.py --limit 5
  python3 ctor_leads.py --doc 2026081900042001     # single document, no merge

Requires: tesseract (brew install tesseract), curl.
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
IMAGE_URL = ("https://a836-acris.nyc.gov/DS/DocumentSearch/GetImage"
             "?doc_id={doc_id}&page={page}")
FIRST_PAGE, LAST_PAGE = 2, 5

# 362(d)(4), tolerant of OCR: d may read as o or 0, brackets may be lost.
# Requires the 4 specifically, so 362(d)(1) and 362(d)(2) do not match.
IN_REM_RE = re.compile(r"362\s*[\(\[]?\s*[D0O]\s*[\)\]]?\s*[\(\[]?\s*4\b"
                       r"|\bIN\s+REM\b")
JFS_RE = re.compile(r"JUDGMENT OF FORECLOSURE AND SALE|FORECLOSURE AND SALE")
INDEX_RE = re.compile(r"INDEX NO\.?\s*(\d{3,6}\s*/\s*\d{2,4})")

NOISE = ("FILED:", "FILED ", "INDEX NO", "NYSCEF", "COUNTY CLERK", "RECEIVED",
         "DOC. NO", "PAGE(S)", "DEPARTMENT OF FINANCE", "SUPREME COURT",
         "STATE OF NEW YORK", "MOTION CALENDAR", "PAPERS NUMBERED")
KEY = re.compile(r"ORDER|JUDGMENT|JUDGEMENT|DECISION|DECREE|STIPULATION")
CERT_TITLE = re.compile(
    r"\d{3,6}\s*/\s*\d{2,4}[\s.,:;|_#-]+([A-Za-z][^\n]{4,100}?)\s*page\(s\)", re.I)
CAPTION = re.compile(r"FOR AN ORDER\s+([A-Za-z][^\n]{4,100})", re.I)


def norm(text):
    t = htmlmod.unescape(text).upper().replace("&", " AND ")
    t = re.sub(r"[^A-Z0-9/()\[\].,'\s-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fetch_page(doc_id, page, tmpdir):
    out = Path(tmpdir) / f"{doc_id}_p{page}.tif"
    url = IMAGE_URL.format(doc_id=doc_id, page=page)
    try:
        subprocess.run(["curl", "-s", "-S", "--max-time", "60", "-A", UA,
                        "-o", str(out), url], capture_output=True, timeout=90)
    except Exception:
        return None
    if not out.exists() or out.stat().st_size < 2000:
        return None
    with open(out, "rb") as fh:
        if fh.read(2) not in (b"II", b"MM"):
            return None
    return out


def ocr(path):
    text = ""
    for psm in (None, "6", "11"):
        cmd = ["tesseract", str(path), "stdout"] + (["--psm", psm] if psm else [])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except Exception:
            return text
        text = r.stdout or ""
        if len(text.strip()) >= 40:
            return text
    return text


def is_stub(text):
    """ACRIS serves a placeholder image past the last real page."""
    return len(text.strip()) < 150 and "THIS IMAGE IS" in text.upper()


def caps_blocks(text):
    """Consecutive capitalized lines: where document titles live."""
    blocks, cur = [], []
    for ln in text.splitlines():
        s = re.sub(r"\s+", " ", ln.strip(" .:_-|~"))
        letters = [c for c in s if c.isalpha()]
        ok = (5 <= len(s) <= 80 and letters
              and not any(n in s.upper() for n in NOISE)
              and sum(1 for c in letters if c.isupper()) / len(letters) >= .85)
        if ok:
            cur.append(s)
        elif cur:
            blocks.append(" ".join(cur))
            cur = []
    if cur:
        blocks.append(" ".join(cur))
    return blocks


def title_of(text):
    """What the document calls itself, verbatim, plus where it was found."""
    generic = re.compile(r"^ORDER[,.\s]*(JUDGMENT)?[,.\s]*(FILED)?[\s\d/]*$", re.I)
    cert = ""
    m = CERT_TITLE.search(text)
    if m:
        cert = m.group(1).strip(" .,;:-")
        if not generic.match(cert):
            return cert, "cert"
    m = CAPTION.search(text)
    if m:
        return ("FOR AN ORDER " + m.group(1)).strip(" .,;:-"), "caption"
    blocks = caps_blocks(text)
    hit = [b for b in blocks if KEY.search(b.upper())]
    if hit:
        return hit[0], "heading"
    if blocks:
        return max(blocks, key=len), "caps"
    return (cert, "cert") if cert else ("", "")


def read_doc(doc_id, tmpdir, sleep):
    """Read pages 2-5, stopping at the stub. Returns the reading fields."""
    row = {"title": "", "title_source": "", "in_rem": "", "in_rem_text": "",
           "jfs": "", "jfs_text": "", "index_no": "", "pages_read": "",
           "read_note": ""}
    blob, pages_read = "", []

    for page in range(FIRST_PAGE, LAST_PAGE + 1):
        img = fetch_page(doc_id, page, tmpdir)
        time.sleep(sleep)
        if img is None:
            row["read_note"] = (row["read_note"] + f" p{page}:no image").strip()
            break
        text = ocr(img)
        if is_stub(text):
            break
        pages_read.append(page)
        blob += "\n" + text

    row["pages_read"] = ",".join(str(p) for p in pages_read)
    if not blob.strip():
        row["read_note"] = (row["read_note"] + " nothing read").strip()
        return row

    title, src = title_of(blob)
    row["title"], row["title_source"] = title, src

    t = norm(blob)
    m = IN_REM_RE.search(t)
    if m:
        row["in_rem"], row["in_rem_text"] = "Y", m.group(0).strip()
    m = JFS_RE.search(t)
    if m:
        row["jfs"], row["jfs_text"] = "Y", m.group(0).strip()
    m = INDEX_RE.search(t)
    if m:
        row["index_no"] = re.sub(r"\s+", "", m.group(1))
    if not title:
        row["read_note"] = (row["read_note"] + " no title found").strip()
    return row

def merge_feed(path, records, keep_days):
    """Fold this run's records into a rolling feed, newest first.

    Keyed by document_id, so re-running an overlapping window updates rows
    rather than duplicating them."""
    feed = {}
    p = Path(path)
    if p.exists():
        try:
            for r in json.load(open(p, encoding="utf-8")).get("records", []):
                feed[r["document_id"]] = r
        except Exception:
            pass
    before = len(feed)
    for r in records:
        feed[r["document_id"]] = r

    def when(r):
        for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"):
            try:
                return datetime.datetime.strptime(r.get("recorded", ""), fmt)
            except ValueError:
                pass
        return datetime.datetime.min

    rows = sorted(feed.values(), key=when, reverse=True)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=keep_days)
    kept = [r for r in rows if when(r) >= cutoff or when(r) == datetime.datetime.min]

    json.dump({"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "count": len(kept),
               "in_rem": sum(1 for r in kept if r.get("in_rem")),
               "jfs": sum(1 for r in kept if r.get("jfs")),
               "records": kept},
              open(p, "w", encoding="utf-8"), indent=1)
    return before, len(kept)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="acris_live.json")
    ap.add_argument("--out", default="leads")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--doc", help="read one document id and stop")
    ap.add_argument("--feed", help="merge results into this rolling feed file")
    ap.add_argument("--feed-days", type=int, default=60,
                    help="drop feed entries older than this many days")
    args = ap.parse_args()

    if subprocess.run(["which", "tesseract"], capture_output=True).returncode:
        sys.exit("tesseract not found on PATH. brew install tesseract")

    if args.doc:
        with tempfile.TemporaryDirectory() as td:
            r = read_doc(args.doc, td, args.sleep)
        for k, v in r.items():
            if v:
                print(f"  {k}: {v}")
        return

    src = Path(args.json)
    if not src.exists():
        sys.exit(f"{src} not found. Run acris_live.py first.")
    payload = json.load(open(src, encoding="utf-8"))
    records = payload.get("records", [])
    if args.limit:
        records = records[:args.limit]
    if not records:
        sys.exit("no records in that file")

    print(f"reading {len(records)} documents\n")
    with tempfile.TemporaryDirectory() as td:
        for n, rec in enumerate(records, 1):
            rec.update(read_doc(rec["document_id"], td, args.sleep))
            flag = ("IN REM" if rec["in_rem"] else
                    ("JFS" if rec["jfs"] else ""))
            print(f"{rec['document_id']}  {flag:<7}"
                  f"{(rec.get('property_address') or '-')[:26]:<28}"
                  f"{(rec['title'] or '(no title)')[:44]}")

    rank = {"in_rem": 0, "jfs": 1, "other": 2}
    records.sort(key=lambda r: rank["in_rem"] if r["in_rem"]
                 else (rank["jfs"] if r["jfs"] else rank["other"]))

    fields = []
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
                   "source_range": payload.get("range"),
                   "count": len(records),
                   "in_rem": sum(1 for r in records if r["in_rem"]),
                   "jfs": sum(1 for r in records if r["jfs"]),
                   "records": records}, fh, indent=1)

    ir = sum(1 for r in records if r["in_rem"])
    jf = sum(1 for r in records if r["jfs"])
    ti = sum(1 for r in records if r["title"])
    print(f"\n{len(records)} documents   in rem: {ir}   foreclosure and sale: {jf}")
    print(f"titled: {ti}/{len(records)}")
    print(f"wrote {args.out}.csv and {args.out}.json")
    if args.feed:
        before, after = merge_feed(args.feed, records, args.feed_days)
        print(f"feed {args.feed}: {before} -> {after} records")


if __name__ == "__main__":
    main()
