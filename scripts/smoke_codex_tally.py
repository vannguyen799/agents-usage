#!/usr/bin/env python3
"""
Pin the Codex tally against synthetic rollouts. No network, no Codex install.

    python3 scripts/smoke_codex_tally.py

Every case here is a mistake that would be INVISIBLE in production: the report still
posts, the row still appears on /usage, and only the number is wrong. Verified against
all 324 real rollouts on the machine this was written on — the deltas reproduce each
session's own cumulative total exactly, compactions included — but those are not
checked in, so this is what keeps it honest.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_codex_usage as reporter  # noqa: E402

FAILURES = []

HOUR = "2026-08-28T04"
"""The hour every event below lands in unless it says otherwise. A tally is keyed by
(model, hour) since 0.8.0 — a session is a span, and one row for all of it can only be
charted as a flat smear over every hour it was open."""


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL {name}\n       got  {got}\n       want {want}")


def meta(**overrides) -> dict:
    payload = {
        "session_id": "01a04682-ba21-7bb3-a175-ab307e3bc0d7",
        "originator": "codex-tui",
        "cwd": "/home/dev/work",
        "model_provider": "openai",
        "git": {"repository_url": "https://github.com/acme/backend.git"},
    }
    payload.update(overrides)
    return {"type": "session_meta", "payload": payload}


def turn(model: str) -> dict:
    return {"type": "turn_context", "payload": {"model": model}}


def tokens(input_tokens: int, cached: int, output: int, reasoning: int = 0,
           at: str = f"{HOUR}:10:00.000Z") -> dict:
    """One `token_count` event, carrying the session's CUMULATIVE totals.

    `at` is the stamp the rollout writes against it, and the hour of that stamp is what
    the delta is filed under. Passing `None` is a record with no usable stamp."""
    record = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": 0,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
            "total_tokens": input_tokens + output,
        },
        "last_token_usage": {},
    }}}
    if at is not None:
        record["timestamp"] = at
    return record


def tally(records: list, accepted=("codex-tui",)) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "rollout-2026-08-28T04-38-35-01a046a9-9c78-79e1-aaad-69371915f2de.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return reporter.tally(path, set(accepted))


def models_of(session: dict) -> dict:
    """Every tally, keyed by (model, hour), with the span stripped off — the counts are
    what most cases here are about, and `startedAt` has its own section."""
    return {
        key: {k: v for k, v in totals.items() if k not in ("startedAt", "endedAt")}
        for key, totals in session.get("models", {}).items()
    }


def counts_of(session: dict, model: str = "gpt-5.6-terra", hour: str = HOUR) -> dict:
    return models_of(session)[(model, hour)]


print("cumulative totals become deltas")
# 3 events of a session that spent 100 uncached + 900 cached input and 60 output.
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(300, 200, 10),
    tokens(900, 700, 30),
    tokens(1500, 1200, 60),
])
check("summed once, not once per turn", models_of(session), {("gpt-5.6-terra", HOUR): {
    "promptTokens": 300, "completionTokens": 60, "cacheReadTokens": 1200, "calls": 3,
}})
check("and the deltas add back up to the last reading",
      sum(counts_of(session)[k] for k in ("promptTokens", "cacheReadTokens")),
      1500)

# A restart is the TOTAL going backwards. One class dipping while the total rises is
# an anomaly, and re-adding the whole cumulative reading for it would be a 2x error.
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(1000, 400, 100),
    tokens(1400, 900, 200),   # uncached input dips 600 -> 500, the total still climbs
])
check("a single class dipping is not a restart", counts_of(session)["cacheReadTokens"], 900)
check("…and the dip contributes nothing rather than everything",
      counts_of(session)["promptTokens"], 600)

print("\nthe cached prefix is not uncached input")
session = tally([meta(), turn("gpt-5.6-terra"), tokens(1_000_000, 990_000, 100)])
check("input minus cached", counts_of(session)["promptTokens"], 10_000)
check("cached reported apart", counts_of(session)["cacheReadTokens"], 990_000)

print("\nreasoning is inside output, not beside it")
session = tally([meta(), turn("gpt-5.6-terra"), tokens(100, 0, 500, reasoning=400)])
check("output not doubled", counts_of(session)["completionTokens"], 500)

print("\na repeated event spends nothing")
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(500, 100, 20), tokens(500, 100, 20), tokens(500, 100, 20),
])
check("identical totals fold into one call", counts_of(session)["calls"], 1)
check("and add nothing", counts_of(session)["promptTokens"], 400)

print("\na model change splits the session")
session = tally([
    meta(), turn("gpt-5.6-terra"), tokens(400, 0, 40),
    turn("gpt-5.6-sol"), tokens(1000, 0, 100),
])
check("each model keeps its own share", models_of(session), {
    ("gpt-5.6-terra", HOUR): {
        "promptTokens": 400, "completionTokens": 40, "cacheReadTokens": 0, "calls": 1},
    ("gpt-5.6-sol", HOUR): {
        "promptTokens": 600, "completionTokens": 60, "cacheReadTokens": 0, "calls": 1},
})

print("\ncompaction restarts the counter without erasing what was spent")
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(10_000, 9_000, 500),
    {"type": "compacted", "payload": {}},
    tokens(2_000, 1_000, 100),   # a fresh count, NOT a decrease to be ignored
    tokens(3_000, 1_500, 150),
])
check("pre- and post-compaction both counted", counts_of(session), {
    "promptTokens": (10_000 - 9_000) + (2_000 - 1_000) + (1_000 - 500),
    "completionTokens": 500 + 100 + 50,
    "cacheReadTokens": 9_000 + 1_000 + 500,
    "calls": 3,
})

print("\nsessions that belong to someone else's meter")
check("a non-interactive originator reports nothing",
      tally([meta(originator="codex-exec"), turn("gpt-5.6-terra"), tokens(500, 0, 50)]), {})
check("…unless it is asked for",
      bool(tally([meta(originator="codex-exec"), turn("gpt-5.6-terra"), tokens(500, 0, 50)],
                 accepted=("codex-tui", "codex-exec"))), True)

print("\nspend nobody named a model for")
check("dropped rather than filed under a guess",
      tally([meta(), tokens(500, 0, 50)]), {})
session = tally([meta(), tokens(500, 0, 50), turn("gpt-5.6-terra"), tokens(800, 0, 80)])
check("but kept when a later turn names one", counts_of(session)["promptTokens"], 800)
session = tally([
    meta(),
    tokens(500, 0, 50, at=f"{HOUR}:10:00.000Z"),
    tokens(900, 0, 90, at="2026-08-28T06:10:00.000Z"),
    turn("gpt-5.6-terra"),
])
check("…each in the hour it was actually spent in, not all in the first",
      {key: totals["promptTokens"] for key, totals in models_of(session).items()},
      {("gpt-5.6-terra", HOUR): 500, ("gpt-5.6-terra", "2026-08-28T06"): 400})

print("\nthe hour a delta was spent in")
# The case the split exists for: one session, left running, spending in two hours.
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(1_000, 0, 100, at=f"{HOUR}:05:00.000Z"),
    tokens(1_000, 0, 100, at=f"{HOUR}:55:00.000Z"),   # a repeat: spends nothing
    tokens(3_000, 0, 300, at="2026-08-28T05:30:00.000Z"),
])
check("a session spanning two hours is two tallies",
      {key: totals["promptTokens"] for key, totals in models_of(session).items()},
      {("gpt-5.6-terra", HOUR): 1_000, ("gpt-5.6-terra", "2026-08-28T05"): 2_000})
check("the span is the events, not the whole hour",
      (session["models"][("gpt-5.6-terra", HOUR)]["startedAt"],
       session["models"][("gpt-5.6-terra", HOUR)]["endedAt"]),
      (f"{HOUR}:05:00.000Z", f"{HOUR}:05:00.000Z"))

print("\nstamps that cannot be read")
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(1_000, 0, 100, at=f"{HOUR}:05:00.000Z"),
    tokens(3_000, 0, 300, at="28/08/2026 05:30"),   # not ISO — unusable
])
check("an unreadable stamp inherits the hour before it, not a new bucket",
      sorted(models_of(session)), [("gpt-5.6-terra", HOUR)])
check("…and does not widen the span it was not part of",
      session["models"][("gpt-5.6-terra", HOUR)]["endedAt"], f"{HOUR}:05:00.000Z")

session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(1_000, 0, 100, at=None), tokens(3_000, 0, 300, at=None),
])
check("a rollout with no stamp anywhere reports the pre-0.8.0 shape",
      sorted(models_of(session)), [("gpt-5.6-terra", "")])
check("…which the wire carries with no bucket at all",
      [set(m) & {"bucket", "startedAt", "endedAt"} for m in reporter.wire_models(session["models"])],
      [set()])

session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(1_000, 0, 100, at=None),                  # before any usable stamp
    tokens(3_000, 0, 300, at="2026-08-28T05:30:00.000Z"),
])
check("spend before the first usable stamp joins the earliest hour there is",
      sorted(models_of(session)), [("gpt-5.6-terra", "2026-08-28T05")])
check("…keeping every token, not just the stamped ones",
      counts_of(session, hour="2026-08-28T05")["promptTokens"], 3_000)

print("\nthe project label")
session = tally([meta(), turn("gpt-5.6-terra"), tokens(1, 0, 1)])
check("read from the recorded remote", session["project"], "acme/backend")
session = tally([meta(git={"repository_url": "https://x-access-token:ghp_secret@github.com/acme/backend.git"}),
                 turn("gpt-5.6-terra"), tokens(1, 0, 1)])
check("a credential in the remote cannot reach it", session["project"], "acme/backend")

print("\nthe backend")
session = tally([meta(), turn("gpt-5.6-terra"), tokens(1, 0, 1)])
check("openai is native", session["backend"], ("firstParty", ""))
session = tally([meta(model_provider="ollama"), turn("gpt-5.6-terra"), tokens(1, 0, 1)])
check("another vendor is not claimed as native", session["backend"], ("custom", ""))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all good")
