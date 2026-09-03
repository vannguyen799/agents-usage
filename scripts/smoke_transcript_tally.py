#!/usr/bin/env python3
"""
Pin the Claude Code tally against synthetic transcripts. No network, no Claude install.

    python3 scripts/smoke_transcript_tally.py

Same standard as `smoke_codex_tally.py`: every case here is a mistake that would be
INVISIBLE in production. The report still posts, the row still appears on /usage, and
only the number — or the hour it is filed under — is wrong.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_usage as reporter  # noqa: E402

FAILURES = []

MODEL = "claude-opus-5"
HOUR = "2026-09-03T07"
"""The hour every response below lands in unless it says otherwise. A tally is keyed by
(model, hour) since 0.8.0 — a session is a span, and one row for the whole of it can
only be charted as an even smear over every hour it was open."""


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL {name}\n       got  {got}\n       want {want}")


def response(msg_id: str, *, at: str = f"{HOUR}:05:00.000Z", model: str = MODEL,
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
    """Every tally, keyed by (model, hour), with the span stripped off."""
    return {
        key: {k: v for k, v in totals.items() if k not in ("startedAt", "endedAt")}
        for key, totals in session["models"].items()
    }


def counts_of(session: dict, hour: str = HOUR) -> dict:
    return models_of(session)[(MODEL, hour)]


print("the hour a response was spent in")
# The case the split exists for: one session, left open, spending in three hours.
session = tally([
    response("msg_1", at=f"{HOUR}:05:00.000Z", input_tokens=100),
    response("msg_2", at=f"{HOUR}:55:00.000Z", input_tokens=200),
    response("msg_3", at="2026-09-03T08:30:00.000Z", input_tokens=400),
    response("msg_4", at="2026-09-03T11:00:00.000Z", input_tokens=800),
])
check("one tally per hour, not one per session",
      {key: totals["promptTokens"] for key, totals in models_of(session).items()},
      {(MODEL, HOUR): 300, (MODEL, "2026-09-03T08"): 400, (MODEL, "2026-09-03T11"): 800})
check("and every token is still there",
      sum(t["promptTokens"] for t in models_of(session).values()), 1500)
check("the span is the responses, not the hour's edges",
      (session["models"][(MODEL, HOUR)]["startedAt"],
       session["models"][(MODEL, HOUR)]["endedAt"]),
      (f"{HOUR}:05:00.000Z", f"{HOUR}:55:00.000Z"))
check("a lone response spans an instant, which the server files whole in its hour",
      session["models"][(MODEL, "2026-09-03T11")]["startedAt"]
      == session["models"][(MODEL, "2026-09-03T11")]["endedAt"], True)

print("\nan idle hour is absent, not zero")
session = tally([
    response("msg_1", at=f"{HOUR}:05:00.000Z"),
    response("msg_2", at="2026-09-03T09:05:00.000Z"),
])
check("the hour nothing happened in gets no tally",
      sorted(h for _, h in models_of(session)), [HOUR, "2026-09-03T09"])

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
    response("msg_1", at=f"{HOUR}:05:00.000Z", input_tokens=100),
    response("msg_2", at="03/09/2026 08:30", input_tokens=400),   # not ISO — unusable
])
check("an unreadable stamp inherits the hour before it, not a new bucket",
      sorted(models_of(session)), [(MODEL, HOUR)])
check("…and does not widen the span it was not part of",
      session["models"][(MODEL, HOUR)]["endedAt"], f"{HOUR}:05:00.000Z")

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
check("spend before the first usable stamp joins the earliest hour there is",
      sorted(models_of(session)), [(MODEL, "2026-09-03T09")])
check("…keeping every token, not just the stamped ones",
      counts_of(session, hour="2026-09-03T09")["promptTokens"], 500)

print("\nthe wire shape")
session = tally([
    response("msg_1", at=f"{HOUR}:05:00.000Z", input_tokens=100),
    response("msg_2", at="2026-09-03T08:30:00.000Z", input_tokens=400),
])
wire = reporter.wire_models(session["models"])
check("one entry per tally, in order", [m["bucket"] for m in wire],
      [f"{HOUR}:00:00Z", "2026-09-03T08:00:00Z"])
check("the bucket is a time, not the 13-character key",
      all(m["bucket"].endswith(":00:00Z") for m in wire), True)
check("every entry carries its own span",
      all(m["startedAt"] and m["endedAt"] for m in wire), True)
check("either all of them name an hour or none does — never a mix",
      len({("bucket" in m) for m in wire}), 1)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all good")
