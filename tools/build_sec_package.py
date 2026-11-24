#!/usr/bin/env python3
"""Build catalog/packages/sec-fin/<version> from an SEC Financial Statement Data Sets quarterly zip.

Stdlib only. Rerunnable and byte-reproducible for a given zip: every insert is sorted, the page
size is fixed and the database is VACUUMed. References come from running each task's gold SQL.
0.1.0 is defined here; each later version's tasks and schema live in their own module (sec_fin_0_2_0, sec_fin_0_3_0).

    python3 tools/build_sec_package.py --version 0.2.0 [--cache-dir DIR] [--user-agent UA]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{q}.zip"
NOTES_URL = "https://www.sec.gov/files/dera/data/financial-statement-notes-data-sets/{m}_notes.zip"
SEGMENTS = re.compile(r"(?:[^=;]+=[^=;]+;)+")   # FSDS segments: "Axis=Member;" pairs, axes in lexical order
FIRST_QUARTER = "2026q3"
ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """\
-- SEC EDGAR Financial Statement Data Sets (FSDS), one quarter, subset to the largest filers.
-- Readme-level metadata only. Tag labels and definitions are not in this database.

-- One row per filing (submission). Only 10-K and 10-Q forms are kept.
CREATE TABLE sub (
  adsh TEXT PRIMARY KEY,  -- accession number, unique filing id
  cik INTEGER,            -- SEC central index key of the registrant
  name TEXT,              -- registrant name as filed (upper case)
  sic INTEGER,            -- standard industrial classification code
  countryba TEXT,         -- country of business address
  form TEXT,              -- submission type: 10-K or 10-Q
  period INTEGER,         -- balance sheet date, YYYYMMDD
  fy INTEGER,             -- fiscal year focus as reported by the filer (may differ from the calendar year)
  fp TEXT,                -- fiscal period focus: FY, Q1, Q2, Q3
  filed INTEGER,          -- filing date, YYYYMMDD
  prevrpt INTEGER         -- 1 if this filing was subsequently amended or replaced, else 0
);

-- One row per reported numeric fact. Only facts with no dimensional qualifier (no segments,
-- no co-registrant) and a standard taxonomy tag are kept.
CREATE TABLE num (
  adsh TEXT,              -- accession number, joins sub.adsh
  tag TEXT,               -- taxonomy element name, joins tag.tag
  version TEXT,           -- taxonomy version, for example us-gaap/2025
  ddate INTEGER,          -- end date of the fact's period, YYYYMMDD (the instant for balance sheet items)
  qtrs INTEGER,           -- duration in quarters rounded: 0 for an instant (point in time), 1, 2, 3, 4 for durations
  uom TEXT,               -- unit of measure, for example USD, shares, USD/shares
  value REAL,             -- reported value in uom (full units, not thousands or millions)
  PRIMARY KEY (adsh, tag, version, ddate, qtrs, uom)
);

-- One row per standard taxonomy element used in num.
CREATE TABLE tag (
  tag TEXT,               -- taxonomy element name
  version TEXT,           -- taxonomy version
  datatype TEXT,          -- monetary, shares, pure, perShare, string, ...
  iord TEXT,              -- I = instant (point in time), D = duration (period of time)
  crdr TEXT,              -- natural balance: C credit, D debit, empty if not applicable
  abstract INTEGER,       -- 1 if abstract (a heading, never reported with a value)
  PRIMARY KEY (tag, version)
);
"""

def fact(company: str, tag: str, qtrs: int) -> str:
    return (f"SELECT n.value FROM sub s JOIN num n ON n.adsh = s.adsh WHERE s.name = '{company}' AND s.form = '10-Q' "
            f"AND s.period = 20260630 AND n.tag = '{tag}' AND n.ddate = 20260630 AND n.qtrs = {qtrs}")


def task(id, group, kind, difficulty, split, question, fmt, sql):
    return dict(id=id, group=group, task=kind, difficulty=difficulty, split=split, question=question, format=fmt, sql=sql)


Q = "its 10-Q whose period ends 2026-06-30"
TASKS: list[dict] = [
    task("lookup_assets", "lookup", "lookup", "simple", "config",
         f"What were the total assets, in US dollars, of EXXON MOBIL CORP on 2026-06-30, as reported in {Q}?",
         "number", fact("EXXON MOBIL CORP", "Assets", 0)),
    task("lookup_goodwill", "lookup", "lookup", "simple", "heldout",
         f"What was the goodwill balance, in US dollars, of COMCAST CORP on 2026-06-30, as reported in {Q}?",
         "number", fact("COMCAST CORP", "Goodwill", 0)),
    task("lookup_tax", "lookup", "lookup", "moderate", "heldout",
         f"What was the income tax expense (benefit), in US dollars, of CATERPILLAR INC for the three months ended 2026-06-30, "
         f"as reported in {Q}? Use the three-month figure, not the six-month figure.",
         "number", fact("CATERPILLAR INC", "IncomeTaxExpenseBenefit", 1)),
    task("lookup_eps", "lookup", "lookup", "moderate", "heldout",
         f"What was the diluted earnings per share, in US dollars per share, of COCA COLA CO for the three months ended 2026-06-30, "
         f"as reported in {Q}? Use the three-month figure, not the six-month figure.",
         "number", fact("COCA COLA CO", "EarningsPerShareDiluted", 1)),
    task("aggregate_count", "aggregate", "aggregation", "simple", "config",
         "How many 10-Q filings with a period ending 2026-06-30 are in the database?",
         "number", "SELECT COUNT(*) FROM sub WHERE form = '10-Q' AND period = 20260630"),
    task("aggregate_top5", "aggregate", "aggregation", "moderate", "heldout",
         "Among filers of a 10-Q with a period ending 2026-06-30, which five reported the largest total assets on 2026-06-30? "
         "Answer with their company names.",
         "names", "SELECT s.name FROM sub s JOIN num n ON n.adsh = s.adsh WHERE s.form = '10-Q' AND s.period = 20260630 "
                  "AND n.tag = 'Assets' AND n.ddate = 20260630 AND n.qtrs = 0 ORDER BY n.value DESC LIMIT 5"),
    task("aggregate_bank_sum", "aggregate", "aggregation", "moderate", "heldout",
         "What was the combined total assets, in US dollars, on 2026-06-30 of all filers with SIC code 6021 (national commercial banks) "
         "that filed a 10-Q with a period ending 2026-06-30?",
         "number", "SELECT SUM(n.value) FROM sub s JOIN num n ON n.adsh = s.adsh WHERE s.form = '10-Q' AND s.period = 20260630 "
                   "AND s.sic = 6021 AND n.tag = 'Assets' AND n.ddate = 20260630 AND n.qtrs = 0"),
    task("aggregate_loss", "aggregate", "aggregation", "challenging", "heldout",
         "Which filers of a 10-Q with a period ending 2026-06-30 reported negative diluted earnings per share for the three months "
         "ended 2026-06-30? Answer with their company names.",
         "names", "SELECT s.name FROM sub s JOIN num n ON n.adsh = s.adsh WHERE s.form = '10-Q' AND s.period = 20260630 "
                  "AND n.tag = 'EarningsPerShareDiluted' AND n.ddate = 20260630 AND n.qtrs = 1 AND n.value < 0 ORDER BY s.name"),
    task("definition_issued", "definition", "concept_mapping", "simple", "config",
         f"How many shares of common stock had EXXON MOBIL CORP issued on 2026-06-30, counting shares it later repurchased and holds "
         f"in treasury, as reported in {Q}?",
         "number", fact("EXXON MOBIL CORP", "CommonStockSharesIssued", 0)),
    task("definition_equity", "definition", "concept_mapping", "moderate", "heldout",
         f"What was the total equity, in US dollars, of ABBOTT LABORATORIES on 2026-06-30, counting both its own shareholders' stake "
         f"and the minority (noncontrolling) owners' stake, as reported in {Q}?",
         "number", fact("ABBOTT LABORATORIES", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", 0)),
    task("definition_cash", "definition", "concept_mapping", "moderate", "heldout",
         f"What was the total of cash, cash equivalents and restricted cash, in US dollars, of BOEING CO on 2026-06-30, "
         f"the balance that reconciles to the statement of cash flows, as reported in {Q}?",
         "number", fact("BOEING CO", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", 0)),
    task("definition_comprehensive", "definition", "concept_mapping", "challenging", "heldout",
         f"What was the total comprehensive income (net income plus other comprehensive income, after tax), in US dollars, "
         f"of AT&T INC. for the three months ended 2026-06-30 that belongs to AT&T's own shareholders, excluding the share "
         f"attributable to noncontrolling interests, as reported in {Q}? Use the three-month figure, not the six-month figure.",
         "number", fact("AT&T INC.", "ComprehensiveIncomeNetOfTax", 1)),
]


def download(url: str, f: Path, ua: str) -> Path:
    if f.is_file() and zipfile.is_zipfile(f):
        return f
    f.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    part = f.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=300) as r, open(part, "wb") as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)
    part.replace(f)
    return f


def fetch_zip(cache: Path, ua: str) -> tuple[Path, str]:
    """Newest available quarter: FIRST_QUARTER, then one quarter earlier at a time."""
    y, n = int(FIRST_QUARTER[:4]), int(FIRST_QUARTER[-1])
    for _ in range(12):
        q = f"{y}q{n}"
        try:
            return download(URL.format(q=q), cache / f"{q}.zip", ua), q
        except urllib.error.HTTPError as e:
            if e.code not in (403, 404):
                raise
            print(f"{q}: HTTP {e.code}, trying the previous quarter", file=sys.stderr)
        y, n = (y, n - 1) if n > 1 else (y - 1, 4)
    raise SystemExit("no FSDS quarter found")


def rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, "utf-8", newline=""), delimiter="\t", quoting=csv.QUOTE_NONE)


def build_db(z: zipfile.ZipFile, db: Path, v: "Version") -> tuple[dict, dict[str, dict], set[tuple[str, str, str]]]:
    """Build the database. Returns the tag definitions, the kept filings by adsh and every (adsh, axis, member) in dim."""
    subs = {r["adsh"]: r for r in rows(z, "sub.txt") if r["form"] in v.forms}
    tags = {(r["tag"], r["version"]): r for r in rows(z, "tag.txt")}
    filing_assets: dict[str, float] = {}
    prints: dict[str, int] = {}
    for r in rows(z, "num.txt"):
        if r["adsh"] not in subs or r["segments"] or r["coreg"] or not r["value"]:
            continue
        if (r["tag"] == "Assets" and r["qtrs"] == "0" and r["uom"] == "USD" and r["version"].startswith("us-gaap/")):
            filing_assets[r["adsh"]] = max(filing_assets.get(r["adsh"], 0.0), float(r["value"]))
        t = tags.get((r["tag"], r["version"]))
        if v.drop_copied_filings and t and t["custom"] == "0":
            key = "\t".join((r["tag"], r["version"], r["ddate"], r["qtrs"], r["uom"], r["value"])).encode()
            prints[r["adsh"]] = (prints.get(r["adsh"], 0) + int.from_bytes(hashlib.blake2b(key, digest_size=16).digest())) % (1 << 128)
    first: dict[int, str] = {}
    for a in sorted(prints, key=lambda a: (subs[a]["accepted"], a)):   # earliest accepted filing of a fact set is kept
        if subs[first.setdefault(prints[a], a)]["cik"] != subs[a]["cik"]:
            del subs[a]
    assets: dict[str, float] = {}
    for a, x in filing_assets.items():
        if a in subs:
            assets[subs[a]["cik"]] = max(assets.get(subs[a]["cik"], 0.0), x)
    ranked = sorted(assets, key=lambda c: (-assets[c], int(c)))
    top = set(ranked[:v.top_n])
    amenders = {r["cik"] for r in subs.values() if r["form"].endswith("/A")}
    top |= set([c for c in ranked if c in amenders and c not in top][:v.amenders])
    subs = {a: r for a, r in subs.items() if r["cik"] in top}
    num, dnum = {}, {}
    for r in rows(z, "num.txt"):
        if r["adsh"] not in subs or not r["value"]:
            continue
        t = tags.get((r["tag"], r["version"]))
        if not t or t["custom"] != "0":
            continue
        k = (r["adsh"], r["tag"], r["version"], int(r["ddate"]), int(r["qtrs"]), r["uom"])
        if not r["segments"] and not r["coreg"]:
            num[k] = float(r["value"])
        elif v.notes_months and r["segments"] and SEGMENTS.fullmatch(r["segments"]):
            dnum[(*k, r["segments"])] = float(r["value"])
    used = sorted({(k[1], k[2]) for k in num} | {(k[1], k[2]) for k in dnum})
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("PRAGMA page_size=4096")
    con.execute("PRAGMA journal_mode=OFF")
    con.executescript(v.schema)
    con.executemany("INSERT INTO sub VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        (a, int(r["cik"]), r["name"], int(r["sic"] or 0), r["countryba"], r["form"], int(r["period"]),
         int(r["fy"] or 0), r["fp"], int(r["filed"]), int(r["prevrpt"] or 0)) for a, r in sorted(subs.items())])
    con.executemany("INSERT INTO num VALUES (?,?,?,?,?,?,?)", [(*k, v) for k, v in sorted(num.items())])
    facts = sorted(dnum.items())
    if v.notes_months:
        con.executemany("INSERT INTO dnum VALUES (?,?,?,?,?,?,?,?,?)",
                        [(i, *k[:6], k[6].count(";"), x) for i, (k, x) in enumerate(facts, 1)])
        con.executemany("INSERT INTO dim VALUES (?,?,?)",
                        [(i, *p.split("=")) for i, (k, _) in enumerate(facts, 1) for p in k[6].split(";") if p])
    con.executemany("INSERT INTO tag VALUES (?,?,?,?,?,?)", [
        (t, ver, tags[t, ver]["datatype"], tags[t, ver]["iord"], tags[t, ver]["crdr"], int(tags[t, ver]["abstract"]))
        for t, ver in used])
    con.commit()
    con.execute("VACUUM")
    con.close()
    defs: dict[str, dict] = {}
    for t, ver in used:   # sorted, so the highest taxonomy version of a tag wins
        defs[t] = {"label": tags[t, ver]["tlabel"], "doc": tags[t, ver]["doc"]}
    pairs = {(k[0], *p.split("=")) for k in dnum for p in k[6].split(";") if p}
    return (standard_definitions(tags, used) if v.standard_labels else defs), subs, pairs


def standard_definitions(tags: dict, used: list[tuple[str, str]]) -> dict[str, dict]:
    """Label and documentation from the newest standard release older than the newest one in the quarter.

    FSDS fills tlabel (and for ifrs also doc) of a taxonomy's newest release in the quarter from a filer's own
    label, for example us-gaap/2026 ProfitLoss "Net loss allocable to common shareholders ...". Older releases
    carry the publisher's text. A tag that only the newest release has gets its doc and an empty label.
    """
    newest: dict[str, str] = {}
    for _, ver in tags:
        if "/" in ver:
            tax, year = ver.split("/")
            newest[tax] = max(newest.get(tax, ""), year)
    defs: dict[str, dict] = {}
    for t, ver in used:
        tax = ver.split("/")[0]
        older = [r for r in (f"{tax}/{y}" for y in range(int(newest[tax]) - 1, 2008, -1)) if (t, r) in tags]
        if older:
            r = tags[t, older[0]]
            defs[t] = {"label": r["tlabel"], "doc": r["doc"]}
        else:
            defs[t] = {"label": "", "doc": tags[t, ver]["doc"]}
    return defs


def short_name(element: str) -> str:
    """An element's name as FSDS segments write it: the "Statement" prefix and an Axis/Member/Domain suffix cut."""
    element = element.removeprefix("Statement")
    for suffix in ("Axis", "Member", "Domain"):
        if element.endswith(suffix):
            return element[:-len(suffix)]
    return element


TAXONOMY_ORDER = ("us-gaap", "srt", "dei", "country", "currency", "exch", "stpr")


def dimension_labels(notes: list[zipfile.ZipFile], subs: dict[str, dict], pairs: set[tuple[str, str, str]]) -> dict:
    """Label and documentation of every axis and member in dim, from the notes data sets' tag.tsv.

    Segments drop namespaces, so a name resolves to the filing's own element (version = its adsh) first, with the
    filer's label, then to a standard element (us-gaap, srt, the rest, ifrs last; Member before Domain) with the
    publisher's text by the definitions.json rule. A name that resolves to nothing (a typed member value) has no entry.
    """
    std: dict[tuple[str, str], dict] = {}
    own: dict[tuple[str, str, str], list[dict]] = {}
    for z in notes:
        for r in rows(z, "tag.tsv"):
            if r["datatype"] in ("axis", "member"):
                if r["custom"] == "1":
                    own.setdefault((r["version"], r["datatype"], short_name(r["tag"])), []).append(r)
                else:
                    std[r["tag"], r["version"]] = r
    by_short: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for (t, ver), r in std.items():
        by_short.setdefault((r["datatype"], short_name(t)), []).append((t, ver))

    def rank(tv: tuple[str, str]):
        tax = tv[1].split("/")[0]
        order = TAXONOMY_ORDER.index(tax) if tax in TAXONOMY_ORDER else len(TAXONOMY_ORDER)
        return tax == "ifrs", order, tv[0].endswith("Domain"), tv[0], tv[1]

    chosen: dict[str, tuple[str, str]] = {}
    filer: dict[str, dict] = {}
    for adsh, axis, member in sorted(pairs):
        for kind, name in (("axis", axis), ("member", member)):
            key = f"{kind}:{name}"
            mine = sorted(own.get((adsh, kind, name), []), key=lambda r: (r["tag"].endswith("Domain"), r["tag"]))
            if mine:
                f = filer.setdefault(adsh, {"name": subs[adsh]["name"], "labels": {}})
                f["labels"][key] = {"element": mine[0]["tag"], "label": mine[0]["tlabel"], "doc": mine[0]["doc"]}
            elif (kind, name) in by_short:
                chosen[key] = min(by_short[kind, name], key=rank)
    defs = standard_definitions(std, sorted(set(chosen.values())))
    return {"standard": {k: {"element": tv[0], **defs[tv[0]]} for k, tv in sorted(chosen.items())}, "filer": filer}


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=True) + "\n", "utf-8")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def gold(con: sqlite3.Connection, t: dict):
    """Run the gold SQL. A number is one row, one column; an unanswerable task's SQL must yield the abstention row."""
    rs = con.execute(t["sql"]).fetchall()
    if t.get("nullable"):
        assert rs == [("NOT_IN_DATABASE",)], f"{t['id']}: the fact must be absent"
        return None
    if t["format"] == "number":
        assert len(rs) == 1 and len(rs[0]) == 1 and rs[0][0] is not None, f"{t['id']}: gold SQL must yield one number"
        return rs[0][0]
    got = [r[0] for r in rs]
    assert got and len(set(got)) == len(got), f"{t['id']}: gold SQL must yield distinct names"
    return got


def write_tasks(con: sqlite3.Connection, pkg: Path, tasks: list[dict]) -> None:
    for d in ("in", "ref"):   # a task dropped from the list leaves no stale files behind
        shutil.rmtree(pkg / "data" / d, ignore_errors=True)
    out = ['version = "1"', ""]
    for t in tasks:
        ref = {"answer": gold(con, t), "answer_format": t["format"], "sql": t["sql"]}
        if "tolerance" in t:
            ref["tolerance"] = t["tolerance"]
        write_json(pkg / "data" / "in" / f"{t['id']}.json", {"answer_format": t["format"], "question": t["question"]})
        write_json(pkg / "data" / "ref" / f"{t['id']}.json", ref)
        tags = ["domain:finance", f"task:{t['task']}", f"difficulty:{t['difficulty']}"]
        out += ["[[task]]", f'task_id = "{t["id"]}"', f'group = "{t["group"]}"', 'tracks = ["semantic_task"]',
                f"tags = {json.dumps(tags)}", f'split = "{t["split"]}"', 'disclosure = "public"',
                f'difficulty = "{t["difficulty"]}"', 'consequence = "low"',
                f'input_ref = "data/in/{t["id"]}.json"', f'reference_ref = "data/ref/{t["id"]}.json"',
                'check = "checks/answer.py:check"']
        out += [f"note = {json.dumps(t['note'])}", ""] if "note" in t else [""]
    (pkg / "tasks.toml").write_text("\n".join(out), "utf-8")


def limitations_0_1_0(quarter: str) -> list[str]:
    return [
        f"Thin vertical slice: {len(TASKS)} tasks, not the planned 100.",
        f"Single quarter of data ({quarter}); one filing season, so period coverage is narrow.",
        "Subset: the 150 filers with the largest reported Assets among 10-K and 10-Q filings; small filers are absent.",
        "Only unsegmented facts with standard (non-custom) taxonomy tags are included; dimensional detail and company extensions are dropped.",
        "Tags subset: task:lookup, task:aggregation and task:concept_mapping only.",
        "analysis.min_groups is 2 for this slice; the full package targets min_groups 8.",
    ]


@dataclass(frozen=True)
class Version:
    forms: tuple[str, ...]
    top_n: int            # the largest filers by reported Assets
    amenders: int         # plus this many of the largest remaining filers that filed a 10-K/A or 10-Q/A
    schema: str
    tasks: list[dict]
    limitations: Callable[[str], list[str]]
    provenance_note: str
    min_groups: int
    drop_copied_filings: bool = False   # drop a filing whose kept facts copy another filer's filing (co-registrant)
    standard_labels: bool = False       # definitions from publisher releases only (see standard_definitions)
    notes_months: tuple[str, ...] = ()  # dimensional facts in dnum/dim, labelled from these notes data sets


def versions() -> dict[str, Version]:
    import sec_fin_0_2_0 as v2
    import sec_fin_0_3_0 as v3
    return {
        "0.1.0": Version(("10-K", "10-Q"), 150, 0, SCHEMA, TASKS, limitations_0_1_0, "", 2),
        "0.2.0": Version(v2.FORMS, v2.TOP_N, v2.AMENDERS, v2.SCHEMA, v2.TASKS, v2.limitations, v2.PROVENANCE_NOTE, 8, True, True),
        "0.3.0": Version(v3.FORMS, v3.TOP_N, v3.AMENDERS, v3.SCHEMA, v3.TASKS, v3.limitations, v3.PROVENANCE_NOTE, 8, True, True,
                         v3.NOTES_MONTHS),
    }


def data_files(v: Version) -> tuple[str, ...]:
    return ("definitions.json",) + (("dimensions.json",) if v.notes_months else ()) + ("fsds.sqlite", "schema.sql")


def write_package_toml(pkg: Path, v: Version, quarter: str, downloaded: str) -> None:
    data = "\n".join(f'[[data]]\npath = "data/{n}"\nsha256 = "{sha(pkg / "data" / n)}"\n' for n in data_files(v))
    lim = v.limitations(quarter)
    prov = f"SEC EDGAR Financial Statement Data Sets {quarter}, {URL.format(q=quarter)}, downloaded {downloaded}{v.provenance_note}"
    budget = "max_usd = 5.0\nbuild_timeout_s = 300\ntask_timeout_s = 120\nmemory_mb = 2048\nmax_attempts = 2\n"
    text = f"""suite = "frozen_core"
provenance = {json.dumps(prov)}
usage_rights = "public domain (US government work)"
taxonomy_version = "1"
controlled_model = ["claude-haiku-5-5", "2026-11"]
permitted_tools = []
external_access = false
human_assistance = "none"
limitations = {json.dumps(lim, indent=2)}

{data}
[budget.controlled]
{budget}
[budget.native]
{budget}
[analysis]
min_meaningful_diff = 0.05
bootstrap_resamples = 2000
seed = 7
min_groups = {v.min_groups}
multiplicity = "holm"
"""
    (pkg / "package.toml").write_text(text, "utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, choices=["0.1.0", "0.2.0", "0.3.0"])
    ap.add_argument("--cache-dir", type=Path, default=Path(tempfile.gettempdir()) / "fsds-cache")
    ap.add_argument("--user-agent", default="observatory-benchmark-builder research@example.org")
    ap.add_argument("--out", type=Path, help="package dir (default catalog/packages/sec-fin/<version>)")
    a = ap.parse_args()
    v = versions()[a.version]
    pkg = a.out or ROOT / "catalog" / "packages" / "sec-fin" / a.version
    zpath, quarter = fetch_zip(a.cache_dir, a.user_agent)
    downloaded = dt.datetime.fromtimestamp(zpath.stat().st_mtime, dt.timezone.utc).strftime("%Y-%m-%d")
    (pkg / "data").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        defs, subs, pairs = build_db(z, pkg / "data" / "fsds.sqlite", v)
    if v.notes_months:
        notes = [zipfile.ZipFile(download(NOTES_URL.format(m=m), a.cache_dir / f"{m}_notes.zip", a.user_agent))
                 for m in v.notes_months]
        write_json(pkg / "data" / "dimensions.json", dimension_labels(notes, subs, pairs))
    (pkg / "data" / "schema.sql").write_text(v.schema, "utf-8")
    write_json(pkg / "data" / "definitions.json", defs)
    con = sqlite3.connect(f"file:{pkg / 'data' / 'fsds.sqlite'}?mode=ro", uri=True)
    write_tasks(con, pkg, v.tasks)
    write_package_toml(pkg, v, quarter, downloaded)
    for n in data_files(v):
        print(f"{sha(pkg / 'data' / n)}  data/{n}")


if __name__ == "__main__":
    main()
