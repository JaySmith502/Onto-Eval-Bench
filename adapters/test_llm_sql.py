"""No network, no key: a local http.server stub plays the Messages API; the adapter runs as a subprocess."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ADAPTER = Path(__file__).with_name("llm_sql.py")
MODEL = ["claude-haiku-5-5", "2026-11"]
SCHEMA = "CREATE TABLE sub(adsh TEXT, name TEXT);\nCREATE TABLE num(adsh TEXT, tag TEXT, value REAL);\n"
DEFS = {"Revenues": {"label": "Revenues", "doc": "Amount of revenue recognized from goods sold."},
        "AssetsCurrent": {"label": "Assets, Current", "doc": "Sum of current assets."},
        "Zebra": {"label": "Zebra Stripes", "doc": "Unrelated."}}


def sql_reply(sql):
    return "```sql\n" + sql + "\n```"


class Stub:
    """Serves queued (status, text) replies; records request bodies."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                stub.requests.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
                code, text = stub.replies.pop(0)
                body = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": MODEL[0],
                                   "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                                   "usage": {"input_tokens": 1000, "output_tokens": 200}} if code == 200
                                  else {"type": "error", "error": {"type": "api_error", "message": "boom"}}).encode()
                self.send_response(code)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inputs = self.tmp / "in"
        self.inputs.mkdir()
        db = sqlite3.connect(self.inputs / "fsds.sqlite")
        db.executescript(SCHEMA + "INSERT INTO sub VALUES ('a','Acme Corp'),('b','Beta  Inc');"
                         "INSERT INTO num VALUES ('a','Revenues',100.5),('b','Revenues',50);")
        db.commit()
        db.close()
        (self.inputs / "schema.sql").write_text(SCHEMA)
        (self.inputs / "definitions.json").write_text(json.dumps(DEFS))
        self.state = self.tmp / "state"

    def call(self, verb, req, stub=None):
        env = {"PATH": os.environ["PATH"], "ANTHROPIC_API_KEY": "test-key",
               "ANTHROPIC_BASE_URL": stub.url if stub else "http://127.0.0.1:9"}
        p = subprocess.run([sys.executable, str(ADAPTER), verb], input=json.dumps(req), capture_output=True,
                           text=True, env=env, cwd=self.tmp, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def build(self, access):
        r = self.call("build", {"state_dir": str(self.state), "model": MODEL, "information_access": access,
                                "inputs": [str(self.inputs / n) for n in ("fsds.sqlite", "schema.sql", "definitions.json")],
                                "settings": {}})
        self.assertEqual(r["status"], "completed")

    def run_task(self, replies, question="total revenues?", fmt="number"):
        stub = Stub(replies)
        self.addCleanup(stub.close)
        r = self.call("run-task", {"state_dir": str(self.state), "model": MODEL, "task_id": "t", "settings": {},
                                   "input": {"question": question, "answer_format": fmt}}, stub)
        return r, stub

    def test_schema_only_excludes_definitions(self):
        self.build(["schema"])
        self.assertFalse((self.state / "definitions_index.json").exists())
        r, stub = self.run_task([(200, sql_reply("SELECT SUM(value) FROM num"))])
        prompt = stub.requests[0]["messages"][0]["content"]
        self.assertIn("CREATE TABLE sub", prompt)
        self.assertNotIn("Amount of revenue", prompt)
        self.assertNotIn("definitions", prompt.lower())
        self.assertEqual(r["output"]["answer"], 150.5)

    def test_definitions_context_includes_retrieved_docs_only(self):
        self.build(["schema", "definitions"])
        _, stub = self.run_task([(200, sql_reply("SELECT 1"))])
        prompt = stub.requests[0]["messages"][0]["content"]
        self.assertIn("Amount of revenue recognized", prompt)
        self.assertNotIn("Unrelated.", prompt)

    def test_repair_turn_once(self):
        self.build(["schema"])
        r, stub = self.run_task([(200, sql_reply("SELECT nope FROM num")), (200, sql_reply("SELECT SUM(value) FROM num"))])
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["output"]["answer"], 150.5)
        self.assertEqual(len(stub.requests), 2)
        self.assertIn("no such column: nope", stub.requests[1]["messages"][-1]["content"])
        self.assertEqual(r["cost"]["tokens_in"], 2000)

    def test_second_failure_is_final(self):
        self.build(["schema"])
        r, stub = self.run_task([(200, sql_reply("SELECT nope FROM num"))] * 2)
        self.assertEqual(r["status"], "invalid_output")
        self.assertEqual(len(stub.requests), 2)
        self.assertEqual(r["cost"]["tokens_out"], 400)

    def test_names_and_number_conversion(self):
        self.build(["schema"])
        r, _ = self.run_task([(200, sql_reply("SELECT name FROM sub ORDER BY adsh;"))], fmt="names")
        self.assertEqual(r["output"], {"answer": ["Acme Corp", "Beta  Inc"], "sql": "SELECT name FROM sub ORDER BY adsh"})
        r, _ = self.run_task([(200, sql_reply("SELECT value FROM num")), (200, sql_reply("SELECT COUNT(*) FROM num"))])
        self.assertEqual(r["output"]["answer"], 2.0)  # multi-row number result triggered the repair turn

    def test_abstention_sentinel_is_a_null_answer(self):
        self.build(["schema"])
        r, stub = self.run_task([(200, sql_reply("SELECT 'NOT_IN_DATABASE'"))])
        self.assertEqual((r["status"], r["output"]["answer"]), ("completed", None))
        self.assertEqual(len(stub.requests), 1)
        r, _ = self.run_task([(200, sql_reply("SELECT 'NOT_IN_DATABASE'"))], fmt="names")
        self.assertEqual((r["status"], r["output"]["answer"]), ("completed", None))

    def test_null_is_a_failed_query_not_an_abstention(self):
        self.build(["schema"])
        r, stub = self.run_task([(200, sql_reply("SELECT SUM(value) FROM num WHERE tag = 'Absent'")),
                                 (200, sql_reply("SELECT 'NOT_IN_DATABASE'"))])
        self.assertIsNone(r["output"]["answer"])
        self.assertIn("NULL", stub.requests[1]["messages"][-1]["content"])
        r, _ = self.run_task([(200, sql_reply("SELECT NULL"))] * 2)
        self.assertEqual(r["status"], "invalid_output")
        r, _ = self.run_task([(200, sql_reply("SELECT NULL"))] * 2, fmt="names")
        self.assertEqual(r["status"], "invalid_output")

    def test_zero_rows_gets_the_repair_turn(self):
        self.build(["schema"])
        r, stub = self.run_task([(200, sql_reply("SELECT value FROM num WHERE tag = 'Absent'")),
                                 (200, sql_reply("SELECT 'NOT_IN_DATABASE'"))])
        self.assertIsNone(r["output"]["answer"])
        self.assertIn("no rows", stub.requests[1]["messages"][-1]["content"])
        r, _ = self.run_task([(200, sql_reply("SELECT name FROM sub WHERE 0"))] * 2, fmt="names")
        self.assertEqual(r["status"], "invalid_output")

    def test_prompt_states_abstention_and_rounding_conventions(self):
        self.build(["schema"])
        _, stub = self.run_task([(200, sql_reply("SELECT 1"))])
        prompt = stub.requests[0]["messages"][0]["content"]
        self.assertIn("SELECT 'NOT_IN_DATABASE'", prompt)
        self.assertIn("ROUND(x, 4)", prompt)

    def test_read_only(self):
        self.build(["schema"])
        r, _ = self.run_task([(200, sql_reply("DELETE FROM num")), (200, sql_reply("DELETE FROM num"))])
        self.assertEqual(r["status"], "invalid_output")
        self.assertEqual(sqlite3.connect(self.state / "fsds.sqlite").execute("SELECT COUNT(*) FROM num").fetchone()[0], 2)

    def test_cost_and_model_used(self):
        self.build(["schema"])
        r, stub = self.run_task([(200, sql_reply("SELECT 1"))])
        self.assertEqual(r["model_used"], MODEL)
        self.assertEqual(r["adapter_version"], "llm-sql-2")
        c = r["cost"]
        self.assertEqual((c["tokens_in"], c["tokens_out"], c["measured"]), (1000, 200, True))
        self.assertAlmostEqual(c["usd"], 1000 * 0.10 / 1e6 + 200 * 0.50 / 1e6)
        self.assertNotIn("temperature", stub.requests[0])
        self.assertEqual(stub.requests[0]["model"], MODEL[0])

    def test_malformed_reply_is_invalid_output(self):
        self.build(["schema"])
        r, stub = self.run_task([(200, "I cannot do that.")] * 2)
        self.assertEqual(r["status"], "invalid_output")
        self.assertIsNone(r["output"])
        self.assertEqual(len(stub.requests), 2)

    def test_api_error_is_runtime_failure(self):
        self.build(["schema"])
        r, _ = self.run_task([(500, "")])
        self.assertEqual(r["status"], "runtime_failure")

    def test_setup_and_teardown_idempotent(self):
        for verb in ("setup", "setup", "teardown"):
            self.assertEqual(self.call(verb, {"state_dir": str(self.state), "settings": {}, "model": MODEL})["status"], "completed")


if __name__ == "__main__":
    unittest.main()
