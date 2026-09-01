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


def tokens(input_tokens: int, cached: int, output: int, reasoning: int = 0) -> dict:
    """One `token_count` event, carrying the session's CUMULATIVE totals."""
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
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


def tally(records: list, accepted=("codex-tui",)) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "rollout-2026-08-28T04-38-35-01a046a9-9c78-79e1-aaad-69371915f2de.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return reporter.tally(path, set(accepted))


def models_of(session: dict) -> dict:
    return session.get("models", {})


print("cumulative totals become deltas")
# 3 events of a session that spent 100 uncached + 900 cached input and 60 output.
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(300, 200, 10),
    tokens(900, 700, 30),
    tokens(1500, 1200, 60),
])
check("summed once, not once per turn", models_of(session), {"gpt-5.6-terra": {
    "promptTokens": 300, "completionTokens": 60, "cacheReadTokens": 1200, "calls": 3,
}})
check("and the deltas add back up to the last reading",
      sum(models_of(session)["gpt-5.6-terra"][k] for k in ("promptTokens", "cacheReadTokens")),
      1500)

# A restart is the TOTAL going backwards. One class dipping while the total rises is
# an anomaly, and re-adding the whole cumulative reading for it would be a 2x error.
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(1000, 400, 100),
    tokens(1400, 900, 200),   # uncached input dips 600 -> 500, the total still climbs
])
check("a single class dipping is not a restart",
      models_of(session)["gpt-5.6-terra"]["cacheReadTokens"], 900)
check("…and the dip contributes nothing rather than everything",
      models_of(session)["gpt-5.6-terra"]["promptTokens"], 600)

print("\nthe cached prefix is not uncached input")
session = tally([meta(), turn("gpt-5.6-terra"), tokens(1_000_000, 990_000, 100)])
check("input minus cached", models_of(session)["gpt-5.6-terra"]["promptTokens"], 10_000)
check("cached reported apart", models_of(session)["gpt-5.6-terra"]["cacheReadTokens"], 990_000)

print("\nreasoning is inside output, not beside it")
session = tally([meta(), turn("gpt-5.6-terra"), tokens(100, 0, 500, reasoning=400)])
check("output not doubled", models_of(session)["gpt-5.6-terra"]["completionTokens"], 500)

print("\na repeated event spends nothing")
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(500, 100, 20), tokens(500, 100, 20), tokens(500, 100, 20),
])
check("identical totals fold into one call", models_of(session)["gpt-5.6-terra"]["calls"], 1)
check("and add nothing", models_of(session)["gpt-5.6-terra"]["promptTokens"], 400)

print("\na model change splits the session")
session = tally([
    meta(), turn("gpt-5.6-terra"), tokens(400, 0, 40),
    turn("gpt-5.6-sol"), tokens(1000, 0, 100),
])
check("each model keeps its own share", models_of(session), {
    "gpt-5.6-terra": {"promptTokens": 400, "completionTokens": 40, "cacheReadTokens": 0, "calls": 1},
    "gpt-5.6-sol": {"promptTokens": 600, "completionTokens": 60, "cacheReadTokens": 0, "calls": 1},
})

print("\ncompaction restarts the counter without erasing what was spent")
session = tally([
    meta(), turn("gpt-5.6-terra"),
    tokens(10_000, 9_000, 500),
    {"type": "compacted", "payload": {}},
    tokens(2_000, 1_000, 100),   # a fresh count, NOT a decrease to be ignored
    tokens(3_000, 1_500, 150),
])
check("pre- and post-compaction both counted", models_of(session)["gpt-5.6-terra"], {
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
check("but kept when a later turn names one",
      models_of(session)["gpt-5.6-terra"]["promptTokens"], 800)

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
