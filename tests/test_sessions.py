import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agw import cli as run

# The columns session_usage reads, as agentgateway 1.7 creates them.
SCHEMA = """
CREATE TABLE request_logs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    http_status INTEGER,
    gen_ai_request_model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost REAL,
    attributes_json TEXT NOT NULL
)
"""


def add_row(con, n, *, session, model="gpt-6-luna", status=200, cost=0.01,
            completed="2026-11-28T10:00:00+00:00", tokens=(100, 10)):
    attributes = {"protocol": "llm"}
    if session is not None:
        attributes["agentgateway.session"] = session
    con.execute(
        "INSERT INTO request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(n), completed, completed, status, model, *tokens, cost,
         json.dumps(attributes)),
    )


class SessionUsageTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(SCHEMA)
        add_row(self.con, 1, session="s-old", completed="2026-11-01T09:00:00+00:00")
        add_row(self.con, 2, session="s-new", completed="2026-11-28T10:00:00+00:00")
        add_row(self.con, 3, session="s-new", model="claude-haiku-5-5",
                completed="2026-11-28T11:00:00+00:00", cost=0.02)
        add_row(self.con, 4, session="s-new", model="gemini-4-neon", cost=None,
                completed="2026-11-28T11:30:00+00:00")
        add_row(self.con, 5, session="s-new", status=401, cost=None, tokens=(None, None),
                completed="2026-11-28T11:40:00+00:00")
        add_row(self.con, 6, session=None)

    def test_lists_sessions_most_recent_first_and_skips_untagged_rows(self):
        rows = run.session_usage(self.con)

        self.assertEqual([r["session"] for r in rows], ["s-new", "s-old"])
        new = rows[0]
        self.assertEqual(new["requests"], 4)
        self.assertEqual(new["errors"], 1)
        self.assertEqual(new["input_tokens"], 300)
        self.assertEqual(new["output_tokens"], 30)
        self.assertAlmostEqual(new["cost"], 0.03)
        # The unpriced Gemini call counts; the failed request doesn't.
        self.assertEqual(new["unpriced"], 1)
        self.assertEqual(new["first"], "2026-11-28T10:00:00+00:00")
        self.assertEqual(new["last"], "2026-11-28T11:40:00+00:00")

    def test_one_session_breaks_down_by_model(self):
        rows = run.session_usage(self.con, session="s-new")

        self.assertEqual(
            [(r["model"], r["requests"]) for r in rows],
            [("gpt-6-luna", 2), ("gemini-4-neon", 1), ("claude-haiku-5-5", 1)],
        )

    def test_since_filters_on_completion_time(self):
        rows = run.session_usage(self.con, since="2026-11-20T00:00:00+00:00")

        self.assertEqual([r["session"] for r in rows], ["s-new"])

    def test_format_marks_lower_bound_costs(self):
        text = run.format_session_usage(run.session_usage(self.con), "session")

        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("session"))
        self.assertIn("0.0300*", lines[1])
        self.assertNotIn("*", lines[2])
        self.assertIn("lower bound", lines[-1])


class CmdSessionsTests(unittest.TestCase):
    def test_reads_database_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "request-logs.db"
            con = sqlite3.connect(db)
            con.execute(SCHEMA)
            add_row(con, 1, session="20261128T163000-ab12cd34ef56")
            con.commit()
            con.close()

            out = io.StringIO()
            with redirect_stdout(out):
                run.cmd_sessions({"request_log_db": str(db)})

            self.assertIn("20261128T163000-ab12cd34ef56", out.getvalue())

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "request-logs.db"

            out = io.StringIO()
            with redirect_stdout(out):
                run.cmd_sessions({"request_log_db": str(db)})

            self.assertIn("doesn't exist yet", out.getvalue())
            self.assertFalse(db.exists())

    def test_unknown_session_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "request-logs.db"
            con = sqlite3.connect(db)
            con.execute(SCHEMA)
            con.commit()
            con.close()

            out = io.StringIO()
            with redirect_stdout(out):
                run.cmd_sessions({"request_log_db": str(db)}, "nope")

            self.assertIn("No request-log rows for session nope", out.getvalue())


class ParserTests(unittest.TestCase):
    def test_sessions_arguments(self):
        args = run.build_parser().parse_args(["sessions", "abc", "--days", "7"])

        self.assertEqual((args.command, args.session, args.days), ("sessions", "abc", 7))


if __name__ == "__main__":
    unittest.main()
