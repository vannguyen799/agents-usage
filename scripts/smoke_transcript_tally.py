#!/usr/bin/env python3
"""
Pin the Claude Code tally against synthetic transcripts. No network, no Claude install.

    python3 scripts/smoke_transcript_tally.py

Same standard as `smoke_codex_tally.py`: every case here is a mistake that would be
INVISIBLE in production. The report still posts, the row still appears on /usage, and
only the number — or the minute it is filed under — is wrong.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_usage as reporter  # noqa: E402

FAILURES = []

MODEL = "claude-opus-5"
MINUTE = "2026-09-03T07:05"
"""The minute every response below lands in unless it says otherwise. A tally is keyed
by (model, minute) since 0.9.0 — a session is a span, and a row covering more than one
chart bucket can only be charted as an even smear over all of them."""


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL {name}\n       got  {got}\n       want {want}")


def response(msg_id: str, *, at: str = f"{MINUTE}:00.000Z", model: str = MODEL,
             entrypoint: str = "cli", input_tokens: int = 100, output: int = 10,
             cache_read: int = 0, write_5m: int = 0, write_1h: int = 0) -> dict:
    """One assistant record as Claude Code writes it."""
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "cache_read_input_tokens": cache_read,
        "cache_creation": {
            "ephemeral_5m_input_tokens": write_5m,
            "ephemeral_1h_input_tokens": write_1h,
        },
    }
    record = {
        "type": "assistant",
        "entrypoint": entrypoint,
        "cwd": "/home/dev/work",
        "message": {"id": msg_id, "model": model, "usage": usage},
    }
    if at is not None:
        record["timestamp"] = at
    return record


def tally(records: list, subagent: list = (), entrypoints=("cli",)) -> dict:
    """Write a transcript (and optionally the subagent transcript it spawned) and read
    it back. The nested path is the one the reporter looks in: `<session>/subagents/`."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "projects" / "-home-dev-work"
        root.mkdir(parents=True)
        path = root / "b154dc52-0774-44d2-8047-6c018b94b622.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        if subagent:
            nested = root / path.stem / "subagents"
            nested.mkdir(parents=True)
            (nested / "agent-1.jsonl").write_text(
                "\n".join(json.dumps(r) for r in subagent) + "\n", encoding="utf-8")
        per_key, skipped, cwd = reporter.tally(path, set(entrypoints))
        return {"models": per_key, "skipped": skipped, "cwd": cwd}


def models_of(session: dict) -> dict:
    """Every tally, keyed by (model, minute), with the span stripped off."""
    return {
        key: {k: v for k, v in totals.items() if k not in ("startedAt", "endedAt")}
        for key, totals in session["models"].items()
    }


def counts_of(session: dict, minute: str = MINUTE) -> dict:
    return models_of(session)[(MODEL, minute)]


print("the minute a response was spent in")
# The case the split exists for: one session, left open, spending in bursts. The two
# responses at :05 and :55 were ONE row in 0.8.x, drawn flat across the fifty minutes
# between them on the minute chart.
session = tally([
    response("msg_1", at=f"{MINUTE}:00.000Z", input_tokens=100),
    response("msg_2", at=f"{MINUTE}:40.000Z", input_tokens=200),
    response("msg_3", at="2026-09-03T07:55:00.000Z", input_tokens=400),
    response("msg_4", at="2026-09-03T11:00:00.000Z", input_tokens=800),
])
check("one tally per minute, not one per hour",
      {key: totals["promptTokens"] for key, totals in models_of(session).items()},
      {(MODEL, MINUTE): 300, (MODEL, "2026-09-03T07:55"): 400, (MODEL, "2026-09-03T11:00"): 800})
check("and every token is still there",
      sum(t["promptTokens"] for t in models_of(session).values()), 1500)
check("the span is the responses, not the minute's edges",
      (session["models"][(MODEL, MINUTE)]["startedAt"],
       session["models"][(MODEL, MINUTE)]["endedAt"]),
      (f"{MINUTE}:00.000Z", f"{MINUTE}:40.000Z"))
check("a lone response spans an instant, which the server files whole in its minute",
      session["models"][(MODEL, "2026-09-03T11:00")]["startedAt"]
      == session["models"][(MODEL, "2026-09-03T11:00")]["endedAt"], True)

print("\nan idle minute is absent, not zero")
session = tally([
    response("msg_1", at=f"{MINUTE}:00.000Z"),
    response("msg_2", at="2026-09-03T07:09:00.000Z"),
])
check("the minutes nothing happened in get no tally",
      sorted(m for _, m in models_of(session)), [MINUTE, "2026-09-03T07:09"])

print("\none response is many lines, and still one call")
# Every content block repeats the whole usage block; summing the lines over-counts 2-3x.
session = tally([
    response("msg_1", input_tokens=100), response("msg_1", input_tokens=100),
    response("msg_1", input_tokens=100),
])
check("deduplicated on the message id", counts_of(session)["promptTokens"], 100)
check("…and counted as one call", counts_of(session)["calls"], 1)

print("\nsubagents spend the parent's quota")
session = tally(
    [response("msg_1", input_tokens=100)],
    subagent=[response("msg_2", input_tokens=700), response("msg_1", input_tokens=100)],
)
check("their tokens are counted", counts_of(session)["promptTokens"], 800)
check("and a response reaching both files is still counted once",
      counts_of(session)["calls"], 2)

print("\nrecords that belong to someone else's meter")
session = tally([
    response("msg_1", input_tokens=100),
    response("msg_2", entrypoint="sdk-cli", input_tokens=900),
])
check("another entrypoint is not reported", counts_of(session)["promptTokens"], 100)
check("…and is counted as skipped rather than passed over in silence",
      session["skipped"], 1)
check("a synthetic turn spent no quota",
      models_of(tally([response("msg_1", model="<synthetic>")])), {})

print("\nthe cache classes are kept apart")
# Folded into one number they cannot be taken apart again, and the weights differ 20x.
session = tally([response("msg_1", input_tokens=10, cache_read=900, write_1h=80, write_5m=8)])
check("each class in its own field", counts_of(session), {
    "promptTokens": 10, "completionTokens": 10,
    "cacheReadTokens": 900, "cacheWrite5mTokens": 8, "cacheWrite1hTokens": 80, "calls": 1,
})

print("\nstamps that cannot be read")
session = tally([
    response("msg_1", at=f"{MINUTE}:00.000Z", input_tokens=100),
    response("msg_2", at="03/09/2026 08:30", input_tokens=400),   # not ISO — unusable
])
check("an unreadable stamp inherits the minute before it, not a new bucket",
      sorted(models_of(session)), [(MODEL, MINUTE)])
check("…and does not widen the span it was not part of",
      session["models"][(MODEL, MINUTE)]["endedAt"], f"{MINUTE}:00.000Z")

session = tally([response("msg_1", at=None), response("msg_2", at=None)])
check("a transcript with no stamp anywhere reports the pre-0.8.0 shape",
      sorted(models_of(session)), [(MODEL, "")])
check("…which the wire carries with no bucket at all",
      [set(m) & {"bucket", "startedAt", "endedAt"} for m in reporter.wire_models(session["models"])],
      [set()])

session = tally([
    response("msg_1", at=None, input_tokens=100),          # before any usable stamp
    response("msg_2", at="2026-09-03T09:05:00.000Z", input_tokens=400),
])
check("spend before the first usable stamp joins the earliest minute there is",
      sorted(models_of(session)), [(MODEL, "2026-09-03T09:05")])
check("…keeping every token, not just the stamped ones",
      counts_of(session, minute="2026-09-03T09:05")["promptTokens"], 500)

print("\nthe wire shape")
session = tally([
    response("msg_1", at=f"{MINUTE}:00.000Z", input_tokens=100),
    response("msg_2", at="2026-09-03T08:30:00.000Z", input_tokens=400),
])
wire = reporter.wire_models(session["models"])
check("one entry per tally, in order", [m["bucket"] for m in wire],
      [f"{MINUTE}:00Z", "2026-09-03T08:30:00Z"])
check("the bucket is a time, not the 16-character key",
      all(len(m["bucket"]) == 20 and m["bucket"].endswith(":00Z") for m in wire), True)
check("every entry carries its own span",
      all(m["startedAt"] and m["endedAt"] for m in wire), True)
check("either all of them name a minute or none does — never a mix",
      len({("bucket" in m) for m in wire}), 1)
# Without it the server floors every bucket to the HOUR and keys 0.8.x rows again.
check("the report says its buckets are minutes",
      reporter.body_of(reporter.load_config(), "s", session["models"], "")["bucketSize"],
      "minute")

print("\na long session never outgrows the server's cap")
# 20 busy hours on one model: 1,200 entries against a cap of 1,024.
per_key = {(MODEL, f"2026-09-03T{h:02d}:{m:02d}"): {"promptTokens": 1, "calls": 1}
           for h in range(20) for m in range(60)}
wire = reporter.wire_models(per_key)
check("it sends no more than the cap", len(wire) <= reporter.MAX_ENTRIES, True)
check("…the newest hours, whole: 03:00 onwards, sixty minutes each",
      (len(wire), wire[0]["bucket"], wire[-1]["bucket"]),
      (17 * 60, "2026-09-03T03:00:00Z", "2026-09-03T19:59:00Z"))

print("\nincremental reading: only new bytes, same totals")
# The hook used to re-read the whole transcript and every subagent file on every turn.
# Now it keeps a byte offset — and the totals it sends must still be exactly the full
# re-read's, or the cumulative contract is broken in a way no single report shows.


class Ledger:
    """Stands in for the platform: applies every posted entry as the server's upsert
    does (replace on (session, model, bucket)), and can be told to fail."""

    def __init__(self):
        self.rows = {}
        self.posts = []
        self.answer = "ok"

    def __call__(self, config, body, user_agent=None):
        self.posts.append(body)
        if self.answer != "ok":
            return self.answer
        for entry in body["models"]:
            self.rows[(entry["model"], entry.get("bucket", ""))] = {
                k: v for k, v in entry.items() if k not in ("model", "bucket")}
        return "ok"


def as_rows(per_key: dict) -> dict:
    return {(e["model"], e.get("bucket", "")): {k: v for k, v in e.items() if k not in ("model", "bucket")}
            for e in reporter.wire_models(per_key, cap=False)}


def append(path: Path, records: list, tail: str = "") -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in records) + tail)


CONFIG = {"entrypoints": {"cli"}, "url": "https://platform.test", "token": "aur_x", "device": "box"}
CONTEXT = {"backend": ["firstParty", ""], "account": "a@b.c"}
consumed = []
real_consume = reporter._consume


def counting_consume(state, mark, line, entrypoints):
    consumed.append(line)
    return real_consume(state, mark, line, entrypoints)


reporter._consume = counting_consume
ledger = Ledger()
REAL_POST_STATUS = reporter.post_status
reporter.post_status = ledger
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory) / "state"
    project = Path(directory) / "projects" / "-home-dev-work"
    project.mkdir(parents=True)
    path = project / "b154dc52-0774-44d2-8047-6c018b94b622.jsonl"
    path.write_text("", encoding="utf-8")

    def report():
        return reporter.report_transcript(CONFIG, "sess", path, CONTEXT, root)

    append(path, [response("msg_1", at=f"{MINUTE}:00.000Z", input_tokens=100),
                  response("msg_2", at="2026-09-03T07:06:00.000Z", input_tokens=50)])
    check("the first run reports the whole session", report(), "ok")
    check("…every minute of it", sorted(ledger.rows), [(MODEL, f"{MINUTE}:00Z"), (MODEL, "2026-09-03T07:06:00Z")])

    consumed.clear()
    ledger.posts.clear()
    check("nothing new: nothing read, nothing sent", (report(), len(consumed), len(ledger.posts)), ("idle", 0, 0))

    # A new response in an existing minute, one in a new minute, and a line still
    # being written (no newline yet) — which must not be consumed and lost.
    partial = json.dumps(response("msg_4", at="2026-09-03T07:08:00.000Z", input_tokens=7))
    append(path, [response("msg_3", at="2026-09-03T07:06:30.000Z", input_tokens=25)], tail=partial[:40])
    consumed.clear()
    ledger.posts.clear()
    report()
    check("only the new line is parsed", len(consumed), 1)
    check("only the minute that changed is sent", [e["bucket"] for e in ledger.posts[0]["models"]],
          ["2026-09-03T07:06:00Z"])
    check("…as its FULL total, never a delta",
          ledger.posts[0]["models"][0]["promptTokens"], 75)

    with path.open("a", encoding="utf-8") as fh:
        fh.write(partial[40:] + "\n")
    report()
    check("the half-written line is picked up once it is whole",
          ledger.rows.get((MODEL, "2026-09-03T07:08:00Z"), {}).get("promptTokens"), 7)

    # A subagent file appearing later, repeating one of the parent's responses.
    nested = project / path.stem / "subagents"
    nested.mkdir(parents=True)
    append(nested / "agent-1.jsonl", [response("msg_9", at="2026-09-03T07:08:10.000Z", input_tokens=300),
                                      response("msg_1", at=f"{MINUTE}:00.000Z", input_tokens=100)])
    report()
    full, _, _ = reporter.tally(path, {"cli"})
    check("after every increment the platform holds exactly the full re-read's rows",
          ledger.rows, as_rows(full))

    # A transcript rewritten under the offset: start over rather than read garbage.
    path.write_text(json.dumps(response("msg_1", at=f"{MINUTE}:00.000Z", input_tokens=100)) + "\n",
                    encoding="utf-8")
    ledger.posts.clear()
    report()
    check("a truncated transcript is re-read whole and re-sent",
          sorted(e["bucket"] for e in ledger.posts[0]["models"]),
          sorted(e["bucket"] for e in reporter.wire_models(reporter.tally(path, {"cli"})[0], cap=False)))

    print("\na report the platform could not take is spooled, not lost")
    ledger.answer = "retry"
    append(path, [response("msg_20", at="2026-09-03T09:00:00.000Z", input_tokens=11)])
    report()
    spooled = list((root / "spool").glob("*.json"))
    check("a failed report lands in the spool", len(spooled), 1)
    append(path, [response("msg_21", at="2026-09-03T09:01:00.000Z", input_tokens=13)])
    report()
    check("the next failure for that session MERGES into the same file",
          sorted(e["bucket"] for e in json.loads(spooled[0].read_text())["models"]),
          ["2026-09-03T09:00:00Z", "2026-09-03T09:01:00Z"])
    ledger.answer = "ok"
    append(path, [response("msg_22", at="2026-09-03T09:00:30.000Z", input_tokens=5)])
    ledger.posts.clear()
    report()
    check("the next success sends the spooled minutes too, in ONE body with the new total",
          {e["bucket"]: e["promptTokens"] for e in ledger.posts[0]["models"]},
          {"2026-09-03T09:00:00Z": 16, "2026-09-03T09:01:00Z": 13})
    check("…and empties the spool", list((root / "spool").glob("*.json")), [])
    # (Compared on the re-read's own keys: the truncation above left a minute on the
    # "server" that the rewritten transcript no longer has, as a real server would.)
    expected = as_rows(reporter.tally(path, {"cli"})[0])
    check("the platform ends up with the full re-read's rows",
          {k: ledger.rows.get(k) for k in expected}, expected)

    ledger.answer = "retry"
    append(path, [response("msg_23", at="2026-09-03T10:00:00.000Z", input_tokens=1)])
    report()
    ledger.answer = "ok"
    reporter.flush_spool(CONFIG, root / "spool")
    check("flush_spool delivers what a session that has ended left behind",
          (list((root / "spool").glob("*.json")), ledger.rows.get((MODEL, "2026-09-03T10:00:00Z"), {}).get("promptTokens")),
          ([], 1))

    ledger.answer = "drop"
    append(path, [response("msg_24", at="2026-09-03T11:00:00.000Z", input_tokens=1)])
    report()
    check("a body the platform refuses as malformed is not spooled forever",
          list((root / "spool").glob("*.json")), [])
    ledger.answer = "ok"

    print("\nthe hook returns at once; one worker drains the queue")
    lock = reporter.WorkerLock(root)
    other = reporter.WorkerLock(root)
    check("the worker lock is exclusive (flock -n)", (lock.acquire(), other.acquire()), (True, False))
    handled = []
    check("a worker that finds the lock taken leaves at once",
          reporter.run_worker(root, CONFIG, lambda p, c: handled.append(p), "ua"), 0)
    lock.release()
    for n in range(3):
        reporter.enqueue(root, {"transcript_path": str(path), "n": n}, CONTEXT)
    reporter.enqueue(root, {"transcript_path": "/elsewhere.jsonl"}, CONTEXT)
    reporter.run_worker(root, CONFIG, lambda p, c: handled.append(p), "ua")
    check("queued payloads collapse to the newest per transcript",
          [(Path(p["transcript_path"]).name, p.get("n")) for p in handled],
          [(path.name, 2), ("elsewhere.jsonl", None)])
    check("…and the queue is left empty", list((root / "queue").glob("*.json")), [])

    # The hook itself: enqueue, start a detached worker, return — no reading, no POST.
    spawned, handled = [], []
    real_spawn = reporter.spawn_worker
    reporter.spawn_worker = lambda script: spawned.append(script)
    try:
        reporter.hook_entry(root, Path("report_usage.py"), {"transcript_path": str(path)}, CONTEXT,
                            CONFIG, lambda p, c: handled.append(p), "ua")
    finally:
        reporter.spawn_worker = real_spawn
    check("the hook only queues and detaches a worker — the session never waits on the report",
          (len(spawned), handled, len(list((root / "queue").glob("*.json")))), (1, [], 1))
    reporter.run_worker(root, CONFIG, lambda p, c: handled.append(c), "ua")
    check("the worker reports with the HOOK's environment, captured when it fired",
          handled, [CONTEXT])
reporter._consume = real_consume

print("\nwhere a report may go")
for url, want in [
    ("https://agents.example.com", True),
    ("http://127.0.0.1:16517", True),
    ("http://localhost:16517", True),
    ("http://[::1]:16517", True),
    ("http://agents.example.com", False),
    ("http://localhost.example.com", False),
    ("ftp://agents.example.com", False),
]:
    check(f"{url} -> {'allowed' if want else 'refused'}", reporter.url_allowed(url), want)
check("an http config is treated as not configured at all",
      reporter.configured({"enabled": True, "url": "http://agents.example.com", "token": "aur_x"}), False)

import email.message  # noqa: E402
import io  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

headers = email.message.Message()
headers["Location"] = "https://elsewhere.example.com/collect"
request = urllib.request.Request("https://agents.example.com/api/v1/usage/sessions", data=b"{}",
                                 method="POST", headers={"authorization": "Bearer aur_x"})
# Driven through the opener's own error chain, exactly as a 307 answer would be: a
# handler that follows returns the redirected response; ours declines and the chain
# ends in HTTPError — which `post_status` spools rather than treating as delivered.
# 301/302/303 are the codes stock urllib turns into a GET that re-sends the header.
for code in (301, 302, 303, 307, 308):
    try:
        followed = reporter._OPENER.error("https", request, io.BytesIO(b""), code, "Moved",
                                          headers) is not None
    except urllib.error.HTTPError as err:
        followed = err.code != code
    check(f"a {code} on the POST is NOT followed (no GET re-sending the Authorization header)",
          followed, False)
check("…and the opener the reporter posts with is the one that refuses",
      any(isinstance(h, reporter._NoRedirect) for h in reporter._OPENER.handlers), True)

print("\nwho paid: a subscription login is quota, an API key is money")
import os  # noqa: E402
saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR")}
real_account = reporter.claude_account_id
try:
    with tempfile.TemporaryDirectory() as directory:
        os.environ["CLAUDE_CONFIG_DIR"] = directory   # an empty settings dir: no apiKeyHelper
        reporter.claude_account_id = lambda: "me@example.com"
        check("a subscription login and no key: quota", reporter.claude_billing(), "subscription")
        os.environ["ANTHROPIC_API_KEY"] = "sk-not-read"
        check("a login AND a key: still quota — the approval that decides it is not read",
              reporter.claude_billing(), "subscription")
        reporter.claude_account_id = lambda: ""
        check("a key and no subscription login: per call", reporter.claude_billing(), "api")
        del os.environ["ANTHROPIC_API_KEY"]
        check("neither: quota, as every report before 0.12.0", reporter.claude_billing(), "subscription")
        (Path(directory) / "settings.json").write_text('{"apiKeyHelper": "~/bin/key.sh"}', encoding="utf-8")
        check("an apiKeyHelper and no login: per call", reporter.claude_billing(), "api")
finally:
    reporter.claude_account_id = real_account
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v

api = reporter.body_of(CONFIG, "sess", {(MODEL, MINUTE): {"promptTokens": 1, "completionTokens": 1, "calls": 1}},
                       "", dict(CONTEXT, billing="api"))
check("a per-call body names the vendor, says billing api, carries no account",
      (api["provider"], api.get("billing"), api["accountId"]), ("anthropic", "api", ""))
quota = reporter.body_of(CONFIG, "sess", {}, "", CONTEXT)
check("a quota body is byte-for-byte what 0.11 sent — no billing key at all",
      ("billing" in quota, quota["provider"]), (False, "claude-cli"))


class Refusal:
    """An opener whose server answers a 400 with the given body."""

    def __init__(self, detail: bytes):
        self.detail = detail

    def open(self, request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", None, io.BytesIO(self.detail))


real_opener = reporter._OPENER
try:
    reporter._OPENER = Refusal(b'{"error":"provider_not_supported:anthropic"}')
    check("a server older than per-call billing refuses it — and the report is KEPT, not dropped",
          REAL_POST_STATUS(CONFIG, api), "retry")
    check("…while the same refusal of a quota body is still a drop",
          REAL_POST_STATUS(CONFIG, quota), "drop")
    reporter._OPENER = Refusal(b'{"error":"backend_not_supported:custom"}')
    check("a NEW server refusing a per-call body for its own reasons is a drop as before",
          REAL_POST_STATUS(CONFIG, api), "drop")
finally:
    reporter._OPENER = real_opener

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all good")
