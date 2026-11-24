"""Baseline text-to-SQL adapter serving both MVP baselines (PRD section 13).

One executable, two ladder levels, chosen by the information_access that `build` receives:
  ["schema"]                  -> baseline-schema:  schema.sql only.
  ["schema", "definitions"]   -> baseline-context: schema.sql plus the 15 definitions whose
                                 keyword overlap with the question is highest.

Definition of the baseline (part of what a score means):
  1. One Messages API call: system prompt + context + question + answer_format, temperature 0
     where the model accepts it (see TEMPERATURE_OK). The model replies with one SQLite SELECT
     in a ```sql block.
  2. The SQL runs read-only (mode=ro, query_only, STATEMENT_LIMIT_S wall limit) and its result is
     converted per answer_format: number = one row, one numeric column; names = first column as
     strings. NULL CONVENTION: a result of exactly one row whose only (number) or first (names)
     column is NULL is the answer null, meaning "the database does not contain this fact". Zero
     rows is not an answer: it is a failed query and gets the repair turn. Rounding is whatever the
     question asks for; the prompt tells the model to apply it in the SQL.
  3. REPAIR TURN. If step 2 fails (SQL error, timeout, zero rows, or a result that cannot be converted) the
     model gets exactly one more turn that quotes the error text and asks for a corrected query.
     A second failure is final. No other retries, no self-consistency, no schema exploration.
Failure statuses: a transport/API failure is runtime_failure; a model that never yields a usable
query (no sql block, not a SELECT, still failing after the repair) is invalid_output, because the
adapter ran correctly and the candidate's output is what was unscorable.

Only files named in build `inputs` are ever read. Stdlib only; runs in python:3.12-slim.
Env: ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL (default https://api.anthropic.com).
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ADAPTER_VERSION = "llm-sql-2"
STATEMENT_LIMIT_S = 30
TOP_K = 15
DOC_CHARS = 500
MAX_TOKENS = 4096
API_VERSION = "2023-06-01"

# USD per million tokens: (input, output, input above 100K prompt tokens, output above 100K).
# Source: claude-api skill "Current Models" table (cached 2026-11-03); Haiku 5.5 long-prompt rates
# are the skill's "$0.50 / $2.50 beyond 100K" note. A model missing here reports measured=False, usd=0.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.50, 2.50),
    "claude-haiku-4-5": (1.00, 5.00, 1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00, 2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00, 3.00, 15.00),
    "claude-opus-5-5": (4.00, 20.00, 4.00, 20.00),
    "claude-opus-5": (5.00, 25.00, 5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00, 5.00, 25.00),
    "claude-fable-5-2": (8.00, 40.00, 8.00, 40.00),
}
# Current-generation models reject non-default sampling parameters with a 400; only these take temperature.
TEMPERATURE_OK = {"claude-haiku-4-5", "claude-sonnet-4-6"}

SYSTEM = ("You translate questions about a SQLite database into one SQLite SELECT statement. "
          "Reply with exactly one ```sql fenced block containing a single read-only SELECT "
          "(a WITH clause is allowed) and nothing else.")
USER = """Database schema:
{schema}
{definitions}
Question: {question}

Answer format: {format_hint}
If the database does not contain the fact the question asks for, return exactly one row with a single NULL (SELECT NULL); that answer means "not in the database". If the question asks for rounding, apply it in the query (for example ROUND(x, 4))."""
DEFINITIONS = """
Relevant US-GAAP tag definitions (tag: label - documentation):
{lines}
"""
FORMAT_HINT = {
    "number": "the query must return exactly one row with one numeric column, in the units stored in num.value.",
    "names": "the query must return company names exactly as stored in sub.name, as the first column, one per row.",
}
REPAIR = """That query failed with this error:
{error}

Return a corrected single SELECT in one ```sql block."""

STOP = set("a an and are as at by for from in is it of on or that the their this to was were what which with how many much did does do total".split())


class Fail(Exception):
    def __init__(self, status, msg, usage=None):
        super().__init__(msg)
        self.status, self.usage = status, usage


def tokens(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP}


def build(req):
    state = Path(req["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    with_defs = "definitions" in req.get("information_access", [])
    by_name = {Path(p).name: Path(p) for p in req["inputs"]}
    for need in ("fsds.sqlite", "schema.sql") + (("definitions.json",) if with_defs else ()):
        if need not in by_name:
            raise Fail("runtime_failure", f"build input {need} missing")
    shutil.copyfile(by_name["fsds.sqlite"], state / "fsds.sqlite")
    shutil.copyfile(by_name["schema.sql"], state / "schema.sql")
    (state / "definitions_index.json").unlink(missing_ok=True)
    if with_defs:
        defs = json.loads(by_name["definitions.json"].read_text())
        index = {t: {"label": d.get("label") or "", "doc": d.get("doc") or "",
                     "tokens": sorted(tokens(f"{t} {d.get('label') or ''} {d.get('doc') or ''}"))}
                 for t, d in defs.items()}
        (state / "definitions_index.json").write_text(json.dumps(index))
    (state / "context.json").write_text(json.dumps({"definitions": with_defs}))


def retrieve(index, question):
    q = tokens(question)
    scored = sorted(((len(q & set(d["tokens"])), t) for t, d in index.items()), key=lambda s: (-s[0], s[1]))
    return [(t, index[t]) for n, t in scored[:TOP_K] if n > 0]


def price(model, tin, tout):
    p = PRICES.get(model)
    if p is None:
        return 0.0, False
    long = tin > 100_000
    return (tin * p[2 if long else 0] + tout * p[3 if long else 1]) / 1e6, True


def call_model(model, messages, usage):
    body = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM, "messages": messages}
    if model in TEMPERATURE_OK:
        body["temperature"] = 0
    r = urllib.request.Request(
        os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages",
        data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "anthropic-version": API_VERSION,
                 "x-api-key": os.environ.get("ANTHROPIC_API_KEY", "")})
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise Fail("runtime_failure", f"API {e.code}: {e.read()[:500]!r}", usage)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        raise Fail("runtime_failure", f"API call failed: {e}", usage)
    u = data.get("usage") or {}
    usage["tin"] += sum(int(u.get(k, 0)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    usage["tout"] += int(u.get("output_tokens", 0))
    usage["model"] = data.get("model") or model
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def extract_sql(reply):
    m = re.search(r"```sql\s*(.*?)```", reply, re.S | re.I)
    if not m:
        raise ValueError("reply has no ```sql block")
    sql = m.group(1).strip().rstrip(";").strip()
    if not re.match(r"(select|with)\b", sql, re.I):
        raise ValueError("query is not a SELECT")
    return sql


def execute(db, sql, fmt):
    conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        deadline = time.monotonic() + STATEMENT_LIMIT_S
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    if not rows:
        raise ValueError("query returned no rows; if the fact is not in the database, return SELECT NULL")
    if fmt == "number":
        if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], (int, float, type(None))):
            raise ValueError(f"number answer needs one row with one numeric column; got {len(rows)} row(s)")
        return None if rows[0][0] is None else float(rows[0][0])
    if len(rows) == 1 and rows[0][0] is None:
        return None
    return [str(r[0]) for r in rows if r[0] is not None]


def run_task(req, usage):
    state = Path(req["state_dir"])
    ctx = json.loads((state / "context.json").read_text())
    inp = req["input"]
    fmt = inp["answer_format"]
    if fmt not in FORMAT_HINT:
        raise Fail("invalid_output", f"unknown answer_format {fmt!r}")
    defs = ""
    if ctx["definitions"]:
        hits = retrieve(json.loads((state / "definitions_index.json").read_text()), inp["question"])
        defs = DEFINITIONS.format(lines="\n".join(f"- {t}: {d['label']} - {d['doc'][:DOC_CHARS]}" for t, d in hits))
    prompt = USER.format(schema=(state / "schema.sql").read_text(), definitions=defs,
                         question=inp["question"], format_hint=FORMAT_HINT[fmt])
    messages = [{"role": "user", "content": prompt}]
    err = ""
    for attempt in (0, 1):
        reply = call_model(req["model"][0], messages, usage)
        try:
            sql = extract_sql(reply)
            return {"answer": execute(state / "fsds.sqlite", sql, fmt), "sql": sql}
        except (ValueError, sqlite3.Error) as e:
            err = f"{type(e).__name__}: {e}"
            messages += [{"role": "assistant", "content": reply}, {"role": "user", "content": REPAIR.format(error=err)}]
    raise Fail("invalid_output", f"no usable query after repair turn: {err}", usage)


def main():
    verb, req = sys.argv[1], json.load(sys.stdin)
    resp = {"status": "completed", "output": None, "adapter_version": ADAPTER_VERSION, "model_used": None}
    usage = {"tin": 0, "tout": 0, "model": (req.get("model") or [""])[0]}
    try:
        if verb == "build":
            build(req)
        elif verb == "run-task":
            resp["output"] = run_task(req, usage)
        elif verb not in ("setup", "teardown"):
            raise Fail("runtime_failure", f"unknown verb {verb}")
    except Fail as f:
        resp["status"] = f.status
        print(f"{verb}: {f}", file=sys.stderr)
    except Exception as e:
        resp["status"] = "runtime_failure"
        print(f"{verb}: {type(e).__name__}: {e}", file=sys.stderr)
    if usage["tin"] + usage["tout"] > 0:
        usd, measured = price(req["model"][0], usage["tin"], usage["tout"])
        resp["cost"] = {"usd": usd, "tokens_in": usage["tin"], "tokens_out": usage["tout"], "measured": measured}
        resp["model_used"] = [usage["model"], req["model"][1]]
    print(json.dumps(resp))


if __name__ == "__main__":
    main()
