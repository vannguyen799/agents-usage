#!/usr/bin/env python3
"""
Pin the OpenCode tally against a synthetic database. No network, no OpenCode install.

    python3 scripts/smoke_opencode_tally.py

Same standard as the other two smokes: every case here is a mistake that would be
INVISIBLE in production — the report still posts, the row still appears on /usage,
and only the number, the minute or the session it is filed under is wrong. The token
semantics were checked against OpenCode 2.0.29's own recorded costs (see the module
docstring of report_opencode_usage.py); the database here only has the columns the
reporter reads.
"""
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_opencode_usage as reporter  # noqa: E402
import report_usage as shared  # noqa: E402

FAILURES = []

OPUS = {"id": "claude-opus-5-5", "providerID": "anthropic"}
GPT = {"id": "gpt-5.6-terra", "providerID": "openai"}
FREE = {"id": "big-pickle", "providerID": "opencode"}

CONFIG = {"url": "https://platform.test", "token": "aur_x", "enabled": True, "device": "box"}
CONTEXT = {"backend": {"claude-cli": ["firstParty", ""], "codex-cli": ["firstParty", ""]}, "envKeys": {}}

SCHEMA = """
CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT, fork_session_id TEXT,
  directory TEXT NOT NULL, model TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
  seq INTEGER NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE account (id TEXT PRIMARY KEY, email TEXT NOT NULL, url TEXT NOT NULL,
  access_token TEXT NOT NULL, refresh_token TEXT NOT NULL);
CREATE TABLE account_state (id INTEGER PRIMARY KEY, active_account_id TEXT);
CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT NOT NULL, value TEXT NOT NULL,
  connector_id TEXT, method_id TEXT, active INTEGER, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
"""

OAUTH = {"anthropic": {"type": "oauth", "methodID": "oauth", "refresh": "SECRET-R", "access": "SECRET-A", "expires": 1},
         "openai": {"type": "oauth", "methodID": "chatgpt-token-sharing", "refresh": "SECRET-R", "access": "SECRET-A",
                    "expires": 1}}
"""What OpenCode 2.0.29 stores for a subscription login — the default for every
database below, so the cases that are not about auth are not skipped for it."""


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL {name}\n       got  {got}\n       want {want}")


def ms(at: str) -> int:
    """`2026-10-15T12:05:10` (UTC) -> epoch milliseconds, the unit OpenCode stores."""
    return int(datetime.fromisoformat(at).replace(tzinfo=timezone.utc).timestamp() * 1000)


class Db:
    """A throwaway opencode.db, written the way OpenCode writes it."""

    def __init__(self, directory: Path, logins: dict = OAUTH):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "opencode.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.seq = {}
        for integration, value in logins.items():
            self.credential(integration, value)

    def credential(self, integration: str, value: dict, active: int = 1):
        """A credential row as 2.0.29 inserts it: the type lives in `value`, next to the
        secret, and `method_id` / `connector_id` stay NULL."""
        if active:
            self.conn.execute("UPDATE credential SET active = 0 WHERE integration_id = ?", (integration,))
        self.conn.execute("INSERT INTO credential VALUES (?,?,?,?,?,?,?,?,?)",
                          (f"cred_{integration}_{len(self.seq)}_{value['type']}_{active}", integration, "x",
                           json.dumps(value), None, None, active, 1, 1))
        self.conn.commit()

    def session(self, sid: str, born: str, parent: str = None, fork: str = None, model: dict = OPUS):
        self.conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
                          (sid, parent, fork, "/nonexistent/acme-backend", json.dumps(model), ms(born), ms(born)))
        self.conn.commit()

    def message(self, mid: str, sid: str, at: str, *, kind: str = "assistant", model=OPUS,
                input_tokens=0, output=0, reasoning=0, read=0, write=0, updated: str = None,
                created_row: str = None, status: str = None):
        """One message row. `created_row` overrides the ROW's time_created — what a
        fork's copy keeps from the original."""
        data = {"time": {"created": ms(at)},
                "tokens": {"input": input_tokens, "output": output, "reasoning": reasoning,
                           "cache": {"read": read, "write": write}}}
        if model is not None:
            data["model"] = model
        if status:
            data["status"] = status
        seq = self.seq[sid] = self.seq.get(sid, 0) + 1
        self.conn.execute("INSERT OR REPLACE INTO session_message VALUES (?,?,?,?,?,?,?)",
                          (mid, sid, kind, seq, ms(created_row or at), ms(updated or at), json.dumps(data)))
        self.conn.commit()

    def rewrite(self, mid: str, updated: str, **tokens):
        """A row rewritten IN PLACE — what OpenCode does while a response streams."""
        data = json.loads(self.conn.execute("SELECT data FROM session_message WHERE id = ?", (mid,)).fetchone()[0])
        data["tokens"].update({k: v for k, v in tokens.items() if k != "read"})
        if "read" in tokens:
            data["tokens"]["cache"]["read"] = tokens["read"]
        self.conn.execute("UPDATE session_message SET data = ?, time_updated = ? WHERE id = ?",
                          (json.dumps(data), ms(updated), mid))
        self.conn.commit()


def tally(db: Db, session: str, env_keys: dict = None) -> dict:
    """Full, stateless read: {provider: {(model, minute): counts}} with spans stripped."""
    with reporter.open_db(db.path) as conn:
        state = reporter.new_state()
        auth = reporter.payers(conn, env_keys or {})
        reporter.advance(conn, state, reporter.root_of(conn, session), auth)
    return {p: {k: {f: v for f, v in t.items() if f not in ("startedAt", "endedAt")} for k, t in per.items()}
            for p, per in reporter.tallies(state).items()}


class Ledger:
    """Stands in for the platform: applies every posted entry as the server's upsert
    does — replace on (session, model, bucket) — and can be told to fail."""

    def __init__(self):
        self.rows, self.posts, self.answer = {}, [], "ok"

    def __call__(self, config, body, user_agent=None):
        self.posts.append(body)
        if self.answer != "ok":
            return self.answer
        for entry in body["models"]:
            self.rows[(body["sessionId"], entry["model"], entry["bucket"])] = {
                k: v for k, v in entry.items() if k not in ("model", "bucket")}
        return "ok"


with tempfile.TemporaryDirectory() as directory:
    tmp = Path(directory)

    print("token classes: OpenCode's semantics, not Codex's")
    db = Db(tmp / "classes")
    db.session("ses_a", "2026-10-15T12:00:00")
    db.message("msg_1", "ses_a", "2026-10-15T12:05:10", input_tokens=2, output=1231, reasoning=422,
               read=384754, write=1873)
    check("reasoning is ADDED to output (stored apart), cache classes kept apart, write is 5-minute",
          tally(db, "ses_a"), {"claude-cli": {("claude-opus-5-5", "2026-10-15T12:05"): {
              "promptTokens": 2, "completionTokens": 1653, "cacheReadTokens": 384754,
              "cacheWrite5mTokens": 1873, "cacheWrite1hTokens": 0, "calls": 1}}})

    db.session("ses_b", "2026-10-15T12:00:00", model=GPT)
    db.message("msg_2", "ses_b", "2026-10-15T12:05:10", model=GPT, input_tokens=500, output=40,
               reasoning=60, read=9000, write=300)
    check("on OpenAI input is NOT reduced by the cache (OpenCode already did), and a write is input",
          tally(db, "ses_b"), {"codex-cli": {("gpt-5.6-terra", "2026-10-15T12:05"): {
              "promptTokens": 800, "completionTokens": 100, "cacheReadTokens": 9000, "calls": 1}}})

    print("\nthe minute a response was spent in")
    db = Db(tmp / "minutes")
    db.session("ses_m", "2026-10-15T12:00:00")
    db.message("msg_1", "ses_m", "2026-10-15T12:05:10", input_tokens=10, output=1)
    db.message("msg_2", "ses_m", "2026-10-15T12:05:50", input_tokens=20, output=2)
    db.message("msg_3", "ses_m", "2026-10-15T13:40:00", input_tokens=40, output=4)
    with reporter.open_db(db.path) as conn:
        state = reporter.new_state()
        reporter.advance(conn, state, "ses_m", reporter.payers(conn, {}))
    per = reporter.tallies(state)["claude-cli"]
    check("one session spanning two hours is two minute rows",
          {k: v["promptTokens"] for k, v in per.items()},
          {("claude-opus-5-5", "2026-10-15T12:05"): 30, ("claude-opus-5-5", "2026-10-15T13:40"): 40})
    check("the span is the responses, not the whole minute",
          (per[("claude-opus-5-5", "2026-10-15T12:05")]["startedAt"],
           per[("claude-opus-5-5", "2026-10-15T12:05")]["endedAt"]),
          ("2026-10-15T12:05:10.000Z", "2026-10-15T12:05:50.000Z"))
    check("the wire carries full ISO buckets and says they are minutes",
          [(e["bucket"], e["calls"]) for e in shared.wire_models(per)],
          [("2026-10-15T12:05:00Z", 2), ("2026-10-15T13:40:00Z", 1)])

    print("\nwhich provider spent it")
    db = Db(tmp / "providers")
    db.session("ses_p", "2026-10-15T12:00:00")
    db.message("msg_1", "ses_p", "2026-10-15T12:05:00", input_tokens=10, output=1)
    db.message("msg_2", "ses_p", "2026-10-15T12:05:30", model=FREE, input_tokens=999, output=99)
    db.message("msg_3", "ses_p", "2026-10-15T12:06:00", model=GPT, input_tokens=50, output=5)
    got = tally(db, "ses_p")
    check("anthropic -> claude-cli, openai -> codex-cli, anything else not at all",
          {p: sorted(k[0] for k in per) for p, per in got.items()},
          {"claude-cli": ["claude-opus-5-5"], "codex-cli": ["gpt-5.6-terra"]})
    check("…and the skipped provider's tokens are nowhere",
          sum(v["promptTokens"] for per in got.values() for v in per.values()), 60)

    print("\nwho paid: a subscription login is quota, an API key is money")
    def mixed(logins):
        db = Db(tmp / f"auth-{len(list(tmp.iterdir()))}", logins)
        db.session("ses_x", "2026-10-15T12:00:00")
        db.message("msg_1", "ses_x", "2026-10-15T12:05:00", input_tokens=10, output=1, write=7)
        db.message("msg_2", "ses_x", "2026-10-15T12:05:30", model=GPT, input_tokens=20, output=2, write=3)
        return db
    KEY = {"type": "key", "key": "SECRET-K"}
    check("an OAuth login on both: both filed as the CLI's quota",
          sorted(tally(mixed(OAUTH), "ses_x")), ["claude-cli", "codex-cli"])
    got = tally(mixed({"anthropic": KEY, "openai": OAUTH["openai"]}), "ses_x")
    check("an Anthropic API KEY (what this box has): filed under the VENDOR, beside the ChatGPT quota",
          sorted(got), ["anthropic", "codex-cli"])
    check("…with Anthropic's five classes, the write a 5-minute one",
          got["anthropic"][("claude-opus-5-5", "2026-10-15T12:05")]["cacheWrite5mTokens"], 7)
    got = tally(mixed({"openai": KEY}), "ses_x")
    check("an OpenAI key: `openai`, the write folded into input as OpenAI bills it",
          (sorted(got), got["openai"][("gpt-5.6-terra", "2026-10-15T12:05")]["promptTokens"]),
          (["openai"], 23))
    check("no credential stored and no key in the environment: nothing reported",
          tally(mixed({}), "ses_x"), {})
    check("no credential but a key in the service's environment: per call",
          sorted(tally(mixed({}), "ses_x", env_keys={"anthropic": True})), ["anthropic"])
    check("a type this release does not know (or `external`): skipped",
          tally(mixed({"anthropic": {"type": "external", "methodID": "x"},
                       "openai": {"type": "wormhole"}}), "ses_x"), {})
    check("an environment key BESIDE an OAuth login is ambiguous: skipped, not guessed",
          sorted(tally(mixed(OAUTH), "ses_x", env_keys={"anthropic": True})), ["codex-cli"])
    db = mixed({"anthropic": KEY})
    db.credential("anthropic", OAUTH["anthropic"], active=0)
    check("the ACTIVE credential decides, not any credential",
          sorted(tally(db, "ses_x")), ["anthropic"])
    with reporter.open_db(db.path) as conn:
        statements = []
        conn.set_trace_callback(statements.append)
        verdict = reporter.payers(conn, {})
    check("…and says why a provider is skipped, for the debug log", verdict["openai"],
          (None, "no openai login stored and no OPENAI_API_KEY"))
    selected = " ".join(statements).replace("json_extract(value, '$.type')", "") \
        .replace("json_extract(value, '$.methodID')", "")
    check("the credential's `value` is never selected whole — only its type and methodID",
          "value" in selected, False)

    print("\nthe per-call wire")
    ledger_api = Ledger()
    shared.post_status = ledger_api
    db = mixed({"anthropic": KEY})
    reporter.report_session(CONFIG, "ses_x", CONTEXT, tmp / "api-state", db.path)
    body = ledger_api.posts[0]
    check("says billing api and names the vendor — what an old server refuses",
          (body["provider"], body.get("billing"), body["accountId"]), ("anthropic", "api", ""))
    check("a quota body carries no billing key at all — byte-for-byte what 0.11 sent",
          "billing" in reporter.body_of(CONFIG, "s", "claude-cli", {}, "", "a@b", CONTEXT), False)

    print("\nsubagents fold into the session that spawned them")
    db = Db(tmp / "tree")
    db.session("ses_root", "2026-10-15T12:00:00")
    db.session("ses_child", "2026-10-15T12:01:00", parent="ses_root")
    db.session("ses_grandchild", "2026-10-15T12:02:00", parent="ses_child")
    db.message("msg_1", "ses_root", "2026-10-15T12:05:00", input_tokens=1, output=1)
    db.message("msg_2", "ses_child", "2026-10-15T12:05:20", input_tokens=10, output=1)
    db.message("msg_3", "ses_grandchild", "2026-10-15T12:05:40", input_tokens=100, output=1)
    with reporter.open_db(db.path) as conn:
        check("a hook naming a grandchild resolves to the root",
              reporter.root_of(conn, "ses_grandchild"), "ses_root")
        check("an unknown session resolves to nothing", reporter.root_of(conn, "ses_nope"), "")
    check("the root's report carries every descendant's spend, once",
          tally(db, "ses_root"), tally(db, "ses_grandchild"))
    check("…summed", tally(db, "ses_root")["claude-cli"][("claude-opus-5-5", "2026-10-15T12:05")]["promptTokens"],
          111)

    print("\na fork does not re-count what it inherited")
    db = Db(tmp / "fork")
    db.session("ses_src", "2026-10-15T12:00:00")
    db.message("msg_1", "ses_src", "2026-10-15T12:05:00", input_tokens=1000, output=100)
    db.session("ses_fork", "2026-10-15T12:30:00", fork="ses_src")
    # OpenCode copies the row whole, original time_created and tokens included.
    db.message("evt_x_1", "ses_fork", "2026-10-15T12:05:00", input_tokens=1000, output=100,
               updated="2026-10-15T12:30:00")
    db.message("msg_2", "ses_fork", "2026-10-15T12:31:00", input_tokens=7, output=1)
    check("only what the fork spent after it was made",
          {k: v["promptTokens"] for k, v in tally(db, "ses_fork")["claude-cli"].items()},
          {("claude-opus-5-5", "2026-10-15T12:31"): 7})
    check("…while the source keeps its own", tally(db, "ses_src")["claude-cli"][
        ("claude-opus-5-5", "2026-10-15T12:05")]["promptTokens"], 1000)

    print("\ncompaction is spend too")
    db = Db(tmp / "compaction")
    db.session("ses_c", "2026-10-15T12:00:00", model=OPUS)
    db.message("cmp_1", "ses_c", "2026-10-15T12:10:00", kind="compaction", status="completed",
               input_tokens=3, output=500, read=90000)
    db.message("cmp_2", "ses_c", "2026-10-15T12:20:00", kind="compaction", status="failed", model=None,
               input_tokens=4, output=0, read=80000)
    got = tally(db, "ses_c")["claude-cli"]
    check("a completed compaction counts on the model it names",
          got[("claude-opus-5-5", "2026-10-15T12:10")]["cacheReadTokens"], 90000)
    check("a failed one, which names none, counts on the session's model",
          got[("claude-opus-5-5", "2026-10-15T12:20")]["cacheReadTokens"], 80000)

    print("\nincremental: only what changed, always as totals")
    ledger = Ledger()
    shared.post_status = ledger
    db = Db(tmp / "incremental")
    state_dir = tmp / "incremental" / "state"
    db.session("ses_i", "2026-10-15T12:00:00")
    db.session("ses_i_child", "2026-10-15T12:00:30", parent="ses_i")
    db.message("msg_1", "ses_i", "2026-10-15T12:05:00", input_tokens=10, output=1)
    db.message("msg_2", "ses_i", "2026-10-15T12:06:00", input_tokens=20, output=2)

    def report(session="ses_i"):
        return reporter.report_session(CONFIG, session, CONTEXT, state_dir, db.path)

    check("the first run reports the whole session", report(), "ok")
    check("…every minute of it, under the root's id",
          sorted(ledger.rows), [("ses_i", "claude-opus-5-5", "2026-10-15T12:05:00Z"),
                                ("ses_i", "claude-opus-5-5", "2026-10-15T12:06:00Z")])
    ledger.posts.clear()
    check("nothing new: nothing sent", (report(), ledger.posts), ("idle", []))

    # The streaming message grows in place; a child adds a response in a new minute.
    db.rewrite("msg_2", "2026-10-15T12:06:30", input=25, output=9)
    db.message("msg_3", "ses_i_child", "2026-10-15T12:08:00", input_tokens=5, output=1)
    ledger.posts.clear()
    report("ses_i_child")
    check("only the minutes that changed are sent — the rewritten one and the new one",
          [e["bucket"] for e in ledger.posts[0]["models"]], ["2026-10-15T12:06:00Z", "2026-10-15T12:08:00Z"])
    check("…the rewritten one as its FULL new total, not added to the old",
          ledger.rows[("ses_i", "claude-opus-5-5", "2026-10-15T12:06:00Z")]["promptTokens"], 25)
    check("…and the child's spend under the root, never under its own id",
          {k[0] for k in ledger.rows}, {"ses_i"})

    # A revert deletes rows — the quota they spent stays spent.
    db.conn.execute("DELETE FROM session_message WHERE id = 'msg_1'")
    db.conn.commit()
    db.message("msg_4", "ses_i", "2026-10-15T12:09:00", input_tokens=1, output=1)
    report()
    check("a reverted message stays counted", ledger.rows[("ses_i", "claude-opus-5-5",
                                                           "2026-10-15T12:05:00Z")]["promptTokens"], 10)
    with reporter.open_db(db.path) as conn:
        full = reporter.new_state()
        reporter.advance(conn, full, "ses_i", reporter.payers(conn, {}))
    expected = {("ses_i", e["model"], e["bucket"]): {k: v for k, v in e.items() if k not in ("model", "bucket")}
                for e in shared.wire_models(reporter.tallies(full)["claude-cli"], cap=False)}
    check("after every increment the platform holds the full re-read's rows (bar the reverted one)",
          {k: v for k, v in ledger.rows.items() if k[2] != "2026-10-15T12:05:00Z"}, expected)

    print("\none body per provider, one spool file per body")
    db.message("msg_5", "ses_i", "2026-10-15T12:10:00", model=GPT, input_tokens=3, output=1)
    db.message("msg_6", "ses_i", "2026-10-15T12:10:30", input_tokens=4, output=1)
    ledger.answer, ledger.posts = "retry", []
    check("a platform that cannot take it now", report(), "retry")
    check("…got one body per provider", sorted(b["provider"] for b in ledger.posts), ["claude-cli", "codex-cli"])
    spooled = sorted(json.loads(p.read_text())["provider"] for p in (state_dir / "spool").glob("*.json"))
    check("…and each is spooled apart, not merged under one provider", spooled, ["claude-cli", "codex-cli"])
    ledger.answer = "ok"
    shared.flush_spool(CONFIG, state_dir / "spool")
    check("the spool delivers both later",
          (list((state_dir / "spool").glob("*.json")),
           ledger.rows[("ses_i", "gpt-5.6-terra", "2026-10-15T12:10:00Z")]["promptTokens"]), ([], 3))

    print("\nwhat a report names")
    db.conn.execute("INSERT INTO account VALUES ('acc_1', 'me@example.com', 'https://opencode.ai', "
                    "'SECRET-ACCESS', 'SECRET-REFRESH')")
    db.conn.execute("INSERT INTO account_state VALUES (1, 'acc_1')")
    db.conn.commit()
    db.message("msg_7", "ses_i", "2026-10-15T12:11:00", input_tokens=1, output=1)
    ledger.posts.clear()
    report()
    body = ledger.posts[0]
    check("the OpenCode login's e-mail is the account", body["accountId"], "me@example.com")
    check("…and nothing else from that table reaches the body", "SECRET" in json.dumps(body), False)
    check("the wire shape", {k: body[k] for k in ("sessionId", "provider", "backend", "bucketSize", "device")},
          {"sessionId": "ses_i", "provider": "claude-cli", "backend": "firstParty",
           "bucketSize": "minute", "device": "box"})

    print("\nthe queue")
    handled = []
    for n in range(3):
        shared.enqueue(state_dir, {"session_id": "ses_one", "n": n}, CONTEXT)
    shared.enqueue(state_dir, {"session_id": "ses_two"}, CONTEXT)
    shared.run_worker(state_dir, CONFIG, lambda p, c: handled.append(p), "ua")
    check("payloads with no transcript collapse per SESSION, not into one shared key",
          [(p["session_id"], p.get("n")) for p in handled], [("ses_one", 2), ("ses_two", None)])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all good")
