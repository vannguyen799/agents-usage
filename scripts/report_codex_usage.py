#!/usr/bin/env python3
"""
Report a Codex CLI session's token usage to the agent platform's usage ledger.

WHY THIS EXISTS
    The same gap the Claude Code reporter closes, on the other CLI. An agent running
    on the platform's `codex-cli` provider is metered: the run writes ledger rows and
    /usage can say whose ChatGPT quota it spent. A human typing into `codex` on the
    same login spends the SAME quota and the platform sees none of it.

WHAT IT READS
    Codex's own rollout — `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`,
    named by the hook on stdin. Three record types carry everything:

        session_meta            first line: session_id, originator, cwd,
                                git.repository_url, model_provider
        turn_context            every turn: the MODEL it ran on (a session can change
                                model mid-way, and then two models spent the quota)
        event_msg/token_count   info.total_token_usage — CUMULATIVE for the session

    What it SENDS is the same wire shape the Claude reporter sends, plus
    `provider: "codex-cli"`: cumulative totals per model, split by rate class,
    upserted on `cli:<sessionId>:<model>`.

THE FOUR TRAPS THIS AVOIDS
    1. `total_token_usage` is CUMULATIVE, so adding up the events multiplies a session
       by its own turn count — a 40-turn session would report ~20x what it spent.
       Tokens are attributed as the DELTA between consecutive events, which is also
       what makes a repeated or a missed event harmless: the deltas always add back up
       to the last total, whatever happened in between.

    2. `input_tokens` INCLUDES `cached_input_tokens`. OpenAI counts the cached prefix
       inside the prompt; Anthropic reports it apart, and the ledger's shape follows
       Anthropic. Passing it through would send the cached half as UNCACHED input,
       which the server weighs at TEN TIMES what a cache read costs — and a Codex
       session is almost entirely cache reads (95.7% of the 904M input tokens across
       316 rollouts on this machine), so the row would read about nine times heavier
       than the work actually was.

    3. `reasoning_output_tokens` is a SUBSET of `output_tokens`, not a fourth class.
       Adding it counts the thinking twice.

    4. Only INTERACTIVE sessions (`originator: "codex-tui"`) are reported. The
       platform runs `codex exec`, and a run it drove is already in the ledger from
       its own side — reporting it here too would bill those tokens twice. That the
       platform passes `--ephemeral`, which persists no rollout at all, is a second
       and independent guard; this filter is what makes the two safe to run on one
       host by construction rather than by coincidence.

Never fails a session: every error path exits 0 and stays quiet.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_usage import (  # noqa: E402  (path first — a hook has no package to import from)
    VERSION, bucketize, device_command, device_of, hour_of, load_config, log, num,
    path_label, post, project_of, safe_url, slug_of, wire_models,
)

USER_AGENT = f"agents-usage/{VERSION} (codex-cli-hook)"

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
"""Codex reads `CODEX_HOME` for everything it stores; so does this."""

CONFIG_PATH = CODEX_HOME / "agents-usage.json"
"""Looked at BEFORE `~/.claude/agents-usage.json`, which is otherwise the shared
default. Installing Codex on a machine that already carries a stale Claude Code config
would otherwise report with the old token and say nothing about why."""

DEFAULT_ORIGINATORS = ("codex-tui",)
"""Session origins worth reporting — see trap 4. Widen with AGENTS_USAGE_ORIGINATORS
(`codex-exec`, `codex-mcp-server`, …) on a host where nothing else meters them."""

NATIVE_PROVIDER = "openai"
"""The `model_provider` of a session talking to OpenAI itself. Anything else is a
different tariff, and the server refuses to weigh it on OpenAI's ratios."""


def originators(config: dict) -> set:
    raw = os.environ.get("AGENTS_USAGE_ORIGINATORS")
    listed = raw.split(",") if raw else config.get("originators") or DEFAULT_ORIGINATORS
    return {str(o).strip() for o in listed if str(o).strip()} or set(DEFAULT_ORIGINATORS)


def codex_account_id() -> str:
    """
    The ChatGPT account whose quota this session spent.

    `tokens.account_id` and nothing else out of `auth.json`. The e-mail is in there
    too, but only inside the `id_token` JWT sitting next to a live refresh token, and
    a reporter that starts taking apart a credential file to find a nicer label is one
    edit away from sending the credential. The id is opaque, stable, and enough for
    /usage to keep one person's spend together.
    """
    try:
        auth = json.loads((CODEX_HOME / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    tokens = auth.get("tokens")
    return str((tokens or {}).get("account_id") or "").strip()


# `device_of` is IMPORTED, not written again here. Both CLIs run on one machine and a
# label derived differently per CLI would split that machine into two rows on the
# /usage breakdown, whose halves look unrelated. Codex's own `installation_id` is gone
# for the same reason, along with Claude Code's `machineID` as a default: a harness's
# id names this box only on that harness. What replaces them is `user@host` and an id
# this plugin generates once into the config BOTH reporters read — see
# report_usage.device_of and report_usage.device_id.


OPENAI_BASE_URL_RE = re.compile(r'^\s*openai_base_url\s*=\s*"([^"]+)"', re.MULTILINE)


def backend_of(model_provider: str) -> tuple:
    """
    WHICH ENDPOINT served this session, as (name, base url).

    Read from the ROLLOUT first — `session_meta.model_provider` is recorded at the
    time, so unlike the Claude reporter (whose transcript names no backend and which
    must read its own environment) this stays right when `--backfill` reports a
    session that ended days ago under a different configuration.

    It matters because the server WEIGHTS these tokens on OpenAI's ratios. A
    `model_provider` other than `openai` is a different vendor's tariff wearing the
    same numbers, and is reported as `custom` so the server can refuse it rather than
    file a figure that looks exactly like every other one and means something else.
    The one thing the rollout cannot record is an `openai` provider pointed at a proxy
    through `openai_base_url`, so that is read from the config — and sanitised, since
    a base URL is an ordinary place to find a credential and this value is sent off
    the machine.
    """
    if model_provider and model_provider != NATIVE_PROVIDER:
        return "custom", ""
    override = os.environ.get("OPENAI_BASE_URL", "").strip() or config_base_url()
    return ("custom", safe_url(override)) if override else ("firstParty", "")


def config_base_url() -> str:
    """`openai_base_url` from `config.toml`, or "" — read with `tomllib` where the
    interpreter has one (3.11+), by pattern where it does not. A plugin that promised
    the standard library and nothing else does not get to require a TOML parser."""
    try:
        raw = (CODEX_HOME / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        import tomllib
        return str(tomllib.loads(raw).get("openai_base_url") or "").strip()
    except Exception:  # no tomllib, or a config this reporter has no business rejecting
        found = OPENAI_BASE_URL_RE.search(raw)
        return found.group(1).strip() if found else ""


def tally(rollout: Path, accepted: set) -> dict:
    """
    One session's cumulative tokens per model, plus what the session says about itself.

    Returns `{}` for a rollout that is not reportable — another originator (trap 4), or
    a file with no session metadata at all.

    THE TOKENS ARE DELTAS OF A CUMULATIVE FIGURE (trap 1), attributed to whichever
    model the last `turn_context` named, so a session that switched model mid-way
    splits across two rows instead of crediting all of it to the one that finished.
    A reading SMALLER than the one before it is a restarted counter, not a negative
    spend — handled where it happens, below.

    Each delta is also filed under the HOUR of the event that carried it, which the
    rollout stamps like everything else. A Codex session runs as long as anyone leaves
    it running, and one row for the whole of it can only be charted as an even smear
    over every hour it was open. The delta already belongs to a moment; this only has
    to stop throwing that moment away.
    """
    meta = {}
    model = ""
    previous = {}
    pending = []
    per_key = {}
    hour = ""

    try:
        handle = rollout.open(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a partially written line while the session is live
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = record.get("type")

            if kind == "session_meta":
                if str(payload.get("originator") or "") not in accepted:
                    log(f"{rollout.name}: skipped — originator "
                        f"{payload.get('originator')!r} is not reported")
                    return {}
                meta = payload
                continue

            if kind == "turn_context":
                model = str(payload.get("model") or "").strip() or model
                # The first turn_context arrives AFTER the first token_count in no
                # rollout seen so far, but the order is the session's to choose:
                # anything counted before a model was named waits here for one.
                if model and pending:
                    for waited in pending:
                        add(per_key, model, *waited)
                    pending = []
                continue

            if kind != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            totals = (info or {}).get("total_token_usage")
            if not isinstance(totals, dict):
                continue

            # An event whose stamp is unusable inherits the hour of the one before it;
            # a rollout is only ever appended to, so its neighbour is the best answer
            # available. Ones before the first usable stamp are placed by `bucketize`.
            hour = hour_of(record.get("timestamp")) or hour
            stamp = str(record.get("timestamp") or "") if hour_of(record.get("timestamp")) else ""

            counts = classes(totals)
            if sum(counts.values()) < sum(previous.values()):
                # The counter RESTARTED. Codex zeroes `total_token_usage` when a
                # session COMPACTS (6 of the 324 rollouts on this machine did), and a
                # fork or a rollback re-using the file would look the same. Everything
                # the new reading holds was spent after that moment, so it becomes the
                # step whole. Dropping it instead — the obvious reading of "ignore a
                # negative delta" — loses the first turn after a compaction, which is
                # the expensive one that re-reads the summary into an empty context.
                step = counts
                log(f"{rollout.name}: token counter restarted (compaction or fork)")
            else:
                # A single class dipping while the total rises is not a restart and
                # must not be treated as one — clamped, so it contributes nothing
                # rather than the whole reading a second time.
                step = {key: max(counts[key] - previous.get(key, 0), 0) for key in counts}
            previous = counts
            if not any(step.values()):
                continue  # a duplicate or a rate-limit-only event — nothing was spent

            if model:
                add(per_key, model, hour, step, stamp)
            else:
                # Held one event per entry rather than folded into a running total:
                # the hour differs between them, and so does the call each one counts.
                pending.append((hour, step, stamp))

    if pending:
        # Spend that no turn_context ever named a model for. Dropped rather than filed
        # under a guess: `model` is half of the upsert key, and a wrong one opens a
        # second row that never converges with the right one.
        held = sum(sum(step.values()) for _, step, _ in pending)
        log(f"{rollout.name}: dropped {held} token(s) — no model named")
    if not per_key or not meta:
        return {}

    return {
        "sessionId": str(meta.get("session_id") or meta.get("id") or "").strip(),
        "project": project_from(meta),
        "backend": backend_of(str(meta.get("model_provider") or "")),
        "models": bucketize(per_key),
    }


def classes(totals: dict) -> dict:
    """
    Codex's cumulative usage block → the ledger's rate classes.

    `input_tokens` includes the cached prefix (trap 2), so uncached input is the
    difference; `reasoning_output_tokens` is already inside `output_tokens` (trap 3)
    and is not added. `cache_write_input_tokens` gets no class of its own: OpenAI
    bills a cache WRITE at the plain input rate, so wherever it lands inside
    `input_tokens` it is already counted exactly once, at weight 1 — which is also
    why the 5m/1h write classes the Claude path fills stay empty here. It has been 0
    in all 324 rollouts on this machine.
    """
    cached = num(totals.get("cached_input_tokens"))
    return {
        "promptTokens": max(num(totals.get("input_tokens")) - cached, 0),
        "completionTokens": num(totals.get("output_tokens")),
        "cacheReadTokens": cached,
    }


def add(per_key: dict, model: str, hour: str, step: dict, stamp: str) -> None:
    """One event's spend, folded into the (model, hour) it belongs to.

    `stamp` narrows the row's span to the events actually inside its hour — compared as
    text, which is the same as comparing the instants while every stamp is ISO-8601 in
    UTC, and `hour_of` accepts nothing else."""
    entry = per_key.setdefault((model, hour), {k: 0 for k in step} | {"calls": 0})
    for key, value in step.items():
        entry[key] = entry.get(key, 0) + value
    entry["calls"] += 1
    if stamp:
        entry["startedAt"] = min(entry.get("startedAt") or stamp, stamp)
        entry["endedAt"] = max(entry.get("endedAt") or stamp, stamp)


def project_from(meta: dict) -> str:
    """
    Which REPO the session ran in.

    Codex records the `origin` URL in the rollout itself, so the repo can be named
    without shelling out to git and without the checkout still existing — which is
    what `--backfill` over months of sessions needs. `slug_of` keeps the URL's PATH
    and drops its authority, so a remote carrying a credential cannot become a label.
    Falls back to the recorded cwd, resolved the same way the Claude reporter does.
    """
    git = meta.get("git")
    remote = str((git or {}).get("repository_url") or "").strip()
    slug = slug_of(remote) if remote else ""
    if slug:
        return slug
    cwd = str(meta.get("cwd") or "").strip()
    if not cwd:
        return ""
    return project_of(cwd) if Path(cwd).is_dir() else path_label(cwd)


def rollouts_of(payload: dict, argv: list) -> list:
    """
    Normally exactly the rollout the hook named.

    `--backfill [days]` sweeps `~/.codex/sessions` instead, which is how a session
    that ended without a final hook gets counted: the report is cumulative and the
    server upserts, so re-reporting one that was already reported is a no-op.
    """
    if "--backfill" in argv:
        index = argv.index("--backfill")
        days = 1.0
        if len(argv) > index + 1 and not argv[index + 1].startswith("-"):
            try:
                days = float(argv[index + 1])
            except ValueError:
                pass
        cutoff = __import__("time").time() - days * 86400
        try:
            found = [p for p in (CODEX_HOME / "sessions").rglob("rollout-*.jsonl")
                     if p.is_file() and p.stat().st_mtime >= cutoff]
        except OSError:
            return []
        return sorted(found, key=lambda p: p.stat().st_mtime)

    path = str(payload.get("transcript_path") or payload.get("transcriptPath") or "").strip()
    if not path:
        # A hook that names no transcript is the one failure this cannot recover from,
        # and it would otherwise look exactly like a session that spent nothing.
        log("no transcript_path in the hook payload — nothing to read")
        return []
    return [Path(path)]


def body_of(config: dict, session: dict, fallback_id: str) -> dict:
    """The wire shape — built in ONE place so `--print` cannot drift from what is sent."""
    backend = session["backend"]
    return {
        "sessionId": session["sessionId"] or fallback_id,
        "provider": "codex-cli",
        "accountId": codex_account_id(),
        "project": session["project"],
        "device": device_of(config),
        "backend": backend[0],
        "baseUrl": backend[1],
        "models": wire_models(session["models"]),
    }


def session_id_of(payload: dict, rollout: Path) -> str:
    """The hook names it; the filename is the fallback. `rollout-<timestamp>-<uuid>`
    ends with the session id, and a UUID is the last five dash-separated groups —
    which is what `--backfill`, running with no hook payload at all, has to go on.
    Only reached when the rollout itself named none, since `tally` prefers that."""
    for key in ("session_id", "sessionId"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return "-".join(rollout.stem.split("-")[-5:])


def main() -> int:
    argv = sys.argv[1:]
    if "--set-device" in argv:
        return device_command(argv, CONFIG_PATH)
    dry_run = "--print" in argv

    payload = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    config = load_config(CONFIG_PATH)
    if not dry_run and not (config["enabled"] and config["url"] and config["token"]):
        log("skipped: not configured (need url + token, and enabled)")
        return 0

    accepted = originators(config)
    for rollout in rollouts_of(payload, argv):
        if not rollout.is_file():
            continue
        session = tally(rollout, accepted)
        if not session:
            continue
        body = body_of(config, session, session_id_of(payload, rollout))
        if not body["sessionId"]:
            continue
        if dry_run:
            print(json.dumps(body, indent=2))
            continue
        post(config, body, USER_AGENT)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # never take a session down with us
        log(f"crashed: {err!r}")
        sys.exit(0)
