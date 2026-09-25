#!/usr/bin/env python3
"""Email the court orders this run added to the feed.

Compares the feed before the run (--old) with the feed after it (--new).
Documents in the new feed that were not in the old one are the new leads.
Builds the CSV with the same rules as the page (one row per person or
company, courts and lenders dropped, entities in Company, sorted by list
first and then newest to oldest) and emails it. Sends nothing when nothing
is new. Never prints names, so the public Actions log stays clean.

Workflow: python3 scripts/csv_email.py --old /tmp/leads_before.json --new leads.json
Mac test: python3 csv_email.py --old OLD.json --new leads.json --dry-run
          python3 csv_email.py --old OLD.json --new leads.json --to you@example.com
"""
import argparse
import csv
import getpass
import io
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage

LISTS = ["Bankruptcy", "Judgment", "Everything else"]
COLS = ["List", "First name", "Last name", "Company", "Property address", "Borough", "Date added"]
SUFFIXES = {"JR", "SR", "II", "III", "IV"}
ENTITY_WORDS = [
    "LLC", "L.L.C.", "INC", "CORP", "CORPORATION", "COMPANY", "LP", "L.P.",
    "LTD", "TRUST", "TRUSTEE", "TRUSTEES", "ESTATE", "COURT", "FUND",
    "MORTGAGE", "HOLDINGS", "PARTNERS", "ASSOCIATES", "ASSOCIATION",
    "ASSOCIATIONS", "ET AL", "BANK", "SAVINGS", "EXECUTOR", "EXECUTRIX",
    "ADMINISTRATOR", "ADMINISTRATRIX", "LODGE", "FUNDING", "SERVICING",
    "CAPITAL", "REALTY", "PROPERTIES", "MANAGEMENT", "DEVELOPMENT", "GROUP",
    "SOCIETY", "CHURCH", "INVESTMENTS", "VENTURES", "AUTHORITY", "DEPARTMENT",
    "CITY OF", "PROPERTY", "PROPERTYIN",
]
ENTITY_RE = re.compile(
    r"(?<![A-Z0-9])(" + "|".join(re.escape(w) for w in ENTITY_WORDS) + r")(?![A-Z0-9])")
DROP_RE = re.compile(
    r"\bCOURT\b|\bU\.?\s?S\.?\s?B\.\s?C\b|\bUNITED STATES OF AMERICA\b|\bBANK\b|\bN\.\s?A\b\.?"
    r"|\bMORTGAGE\b|\bSAVINGS\b|\bLOAN\b|\bLENDER\b|\bSERVICING\b|\bISSUER\b")


def load(path):
    d = json.load(open(path, encoding="utf-8"))
    r = d if isinstance(d, list) else (next((v for v in d.values() if isinstance(v, list)), None) or [])
    return [x for x in r if isinstance(x, dict) and x.get("document_id")]


def norm(s):
    return re.sub(r"\s+", " ", str(s or "").upper()).strip()


def split_name(name):
    """(first, last, is_person); entities come back whole in last."""
    if "," not in name or ENTITY_RE.search(name):
        return "", name, False
    parts = [p.strip() for p in name.split(",")]
    last, rest = parts[0], [p for p in parts[1:] if p]
    while rest and rest[0].replace(".", "") in SUFFIXES:
        last += " " + rest.pop(0).replace(".", "")
    while len(rest) > 1 and rest[-1].replace(".", "") in SUFFIXES:
        last += " " + rest.pop().replace(".", "")
    first = ", ".join(rest)
    toks = first.split()
    if len(toks) > 1 and toks[-1].replace(".", "") in SUFFIXES:
        last += " " + toks.pop().replace(".", "")
        first = " ".join(toks)
    return (first, last, True) if first else ("", last, False)


def date_of(s):
    try:
        return datetime.strptime(str(s or "").split(" ")[0], "%m/%d/%Y")
    except ValueError:
        return datetime.min


def list_for(rec):
    if rec.get("in_rem"):
        return "Bankruptcy"
    if rec.get("jfs"):
        return "Judgment"
    return "Everything else"


def build_rows(recs):
    best, rows = {}, []
    for rec in sorted(recs, key=lambda r: date_of(r.get("recorded")), reverse=True):
        for key in ("p1_name", "p2_name", "p3_name"):
            name = norm(rec.get(key))
            if not name or DROP_RE.search(name):
                continue
            first, last, is_person = split_name(name)
            row = {
                "List": list_for(rec),
                "First name": first if is_person else "",
                "Last name": last if is_person else "",
                "Company": "" if is_person else name,
                "Property address": (rec.get("property_address") or "").strip(),
                "Borough": rec.get("borough") or "",
                "Date added": str(rec.get("recorded") or "").split(" ")[0],
            }
            k = (name, norm(row["Property address"]))
            cur = best.get(k)
            if cur is None:
                best[k] = row
                rows.append(row)
            elif LISTS.index(row["List"]) < LISTS.index(cur["List"]):
                cur["List"] = row["List"]
    rows.sort(key=lambda w: (LISTS.index(w["List"]), date_of(w["Date added"]) == datetime.min,
                             -date_of(w["Date added"]).toordinal() if date_of(w["Date added"]) != datetime.min else 0))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="feed before this run")
    ap.add_argument("--new", required=True, help="feed after this run")
    ap.add_argument("--to", help="override recipients (comma-separated); default is EMAIL_TO")
    ap.add_argument("--dry-run", action="store_true", help="write the CSV locally, send nothing")
    args = ap.parse_args()

    if not os.path.exists(args.old):
        sys.exit(f"{args.old} not found; refusing to treat the whole feed as new.")
    old_ids = {r["document_id"] for r in load(args.old)}
    new_docs = [r for r in load(args.new) if r["document_id"] not in old_ids]
    if not new_docs:
        print("no new court orders this run; no email sent")
        return

    rows = build_rows(new_docs)
    counts = {k: sum(1 for w in rows if w["List"] == k) for k in LISTS}
    today = datetime.now().strftime("%Y-%m-%d")
    fname = f"court_orders_{today}.csv"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS)
    w.writeheader()
    w.writerows(rows)

    print(f"{len(new_docs)} new court orders -> {len(rows)} CSV rows {counts}")
    if args.dry_run:
        with open(fname, "w", newline="", encoding="utf-8") as f:
            f.write(buf.getvalue())
        print(f"dry run: wrote {fname}, sent nothing")
        return

    user = os.environ.get("GMAIL_USER") or "brant.g.4ev@gmail.com"
    pw = os.environ.get("GMAIL_APP_PASSWORD") or getpass.getpass("App password: ")
    to = [a.strip() for a in (args.to or os.environ.get("EMAIL_TO", "")).split(",") if a.strip()]
    if not to:
        sys.exit("no recipients: set EMAIL_TO or pass --to")

    m = EmailMessage()
    m["Subject"] = f"({today}): {len(new_docs)} new ACRIS court orders"
    m["From"] = user
    m["To"] = ", ".join(to)
    m.set_content(
        f"{len(new_docs)} new court orders in latest data fetch. {len(rows)} rows attached.\n\n"
        + "\n".join(f"{k}: {v}" for k, v in counts.items())
        + "\n\nSorted first by list, then sorted newest to oldest.\n")
    m.add_attachment(buf.getvalue().encode("utf-8"), maintype="text", subtype="csv", filename=fname)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw.replace(" ", ""))
        s.send_message(m)
    print(f"sent to {len(to)} recipient(s)")


if __name__ == "__main__":
    main()
