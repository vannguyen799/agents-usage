#!/usr/bin/env python3
"""
Report an OpenCode session's token usage to the agent platform's usage ledger.

WHY THIS EXISTS
    The same gap the Claude Code and Codex reporters close, on a third harness.
    OpenCode signed in with a Claude or ChatGPT SUBSCRIPTION login spends the same
    Pro/Max/Plus quota as the vendor's own CLI, and the platform sees none of it. Its rows are filed under
    the CLI whose quota they spent — `claude-cli` for Anthropic, `codex-cli` for
    OpenAI — so /usage shows one subscription's burn on one line whichever harness
    spent it.

WHAT IT READS
    OpenCode 2.x keeps every session in ONE SQLite database,
    `$XDG_DATA_HOME/opencode/opencode.db`, opened READ-ONLY (it is in WAL mode, and
    OpenCode's background service holds it open the whole time):

        session_v2        one row per session: `parent_id` (a subagent's parent),
                          `fork_session_id`, `directory`, `model`, `time_created`
        session_message   one row per message, its body in a `data` JSON column.
                          Two types carry spend: `assistant` (one model response:
                          `model` + `tokens`) and `compaction` (the summary call).

    Title generation is the one spend recorded nowhere per message — it is folded
    into `session_v2`'s own totals with no model attached — so it is not reported:
    about a thousand tokens per session or less, against a model nothing names.

THE SIX TRAPS THIS AVOIDS
    1. `tokens.output` EXCLUDES `tokens.reasoning` — the opposite of Codex, where
       reasoning is a subset of output. OpenCode stores `output - reasoning` as
       "visible" output and prices `(output + reasoning)` at the output rate, so the
       ledger's output is the SUM. Fitted against the cost OpenCode itself recorded
       on 153 Anthropic messages, output and reasoning come out at the same rate,
       to the cent, as two separate addends.

    2. `tokens.input` excludes BOTH cache classes, for every provider — OpenCode
       normalises OpenAI's cached-inclusive `input_tokens` before storing it. So
       nothing is subtracted here (doing what the Codex reporter does would take the
       cache out twice). `cache.write` is a 5-MINUTE write on Anthropic — OpenCode's
       default cache hint names no TTL, and the recorded costs price it at exactly
       1.25x input — and on OpenAI it is folded back into input, which is what
       OpenAI bills a cache write at.

    3. A FORK copies the session it forked from — every message, `tokens` and all —
       into the new session's rows, keeping each copy's original `time_created`.
       OpenCode starts the fork's own totals at zero; so does this, by skipping any
       row older than the session it sits in. A session cannot have spent tokens
       before it existed.

    4. Rows are UPDATED IN PLACE while a response streams, so a reading keyed on
       `seq` alone would freeze a message at whatever it held when first seen. Rows
       are re-read by `time_updated`, and each message's contribution is REPLACED by
       its id, never added a second time.

    5. Only Anthropic and OpenAI are reported (`providerID` `anthropic` / `openai`).
       Anything else — OpenCode's own free models, OpenRouter, an API-key gateway —
       spends no subscription the platform meters, and the server would refuse the
       provider anyway. Skipped, and said so under AGENTS_USAGE_DEBUG.

    6. ...and only when OpenCode is signed in to that provider with a SUBSCRIPTION
       login. The rows are filed as `claude-cli` / `codex-cli` at $0, i.e. as Pro/Max
       or ChatGPT QUOTA; a session OpenCode ran on an API key was billed per call
       and is not that, however identical its tokens look. See `subscriptions`.

NO ORIGIN FILTER
    Unlike Claude Code (`entrypoint`) and Codex (`originator`), every OpenCode
    session is reported however it was started. The one program on this side that
    drives OpenCode — agent-tool-gateway, over `opencode acp` — meters nothing to
    the platform itself, so there is no second ledger write to double.

Never fails a session: every error path exits 0 and stays quiet.
"""
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_usage import (  # noqa: E402  (path first — a hook has no package to import from)
    VERSION, WorkerLock, _key_of, configured, deliver, device_command, device_of,
    flush_spool, hook_entry, load_config, log, num, path_label, project_of, read_json,
    read_payload, run_worker, safe_url, state_root, wire_models, write_json,
)

USER_AGENT = f"agents-usage/{VERSION} (opencode-plugin)"


def xdg(var: str, default: str) -> Path:
    """An XDG base directory — OpenCode resolves its own paths exactly this way."""
    named = os.environ.get(var, "").strip()
    return Path(named) if named else Path.home() / default


CONFIG_PATH = xdg("XDG_CONFIG_HOME", ".config") / "opencode" / "agents-usage.json"
"""Looked at BEFORE the shared `~/.claude/agents-usage.json`, the way Codex puts its
own directory first: a machine that installs OpenCode beside a stale Claude Code
config reports with the token just written rather than silently with the old one."""

PROVIDERS = {"anthropic": "claude-cli", "openai": "codex-cli"}
"""OpenCode `providerID` -> the CLI whose subscription quota it spent (trap 5)."""

SPEND_TYPES = ("assistant", "compaction")

BASE_URL_ENV = {"anthropic": "ANTHROPIC_BASE_URL", "openai": "OPENAI_BASE_URL"}


def db_path() -> Path:
    return xdg("XDG_DATA_HOME", ".local/share") / "opencode" / "opencode.db"


def open_db(db: Path) -> sqlite3.Connection:
    """
    Read-only, through the URI, so this can never create, migrate or write the file
    OpenCode owns. WAL lets it read beside the live service without blocking it; the
    timeout only covers the moment a checkpoint holds the lock.
    """
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


# ------------------------------------------------------------------------------------
# What a session is: its subagents fold in, a fork's inheritance does not
# ------------------------------------------------------------------------------------

def root_of(conn: sqlite3.Connection, session_id: str) -> str:
    """
    The top of the session tree `session_id` belongs to; "" when it does not exist.

    A subagent is a session of its own (`parent_id` names who spawned it), but it is
    reported INSIDE its root — the session a person or a driver actually started —
    the way Claude Code's subagent transcripts fold into theirs. One rule, applied to
    every hook whichever session it names, so a child's spend is filed under exactly
    one id and never under two. A parent that has since been deleted makes the
    highest surviving ancestor the root.
    """
    found, current, seen = "", session_id, set()
    while current and current not in seen:
        seen.add(current)
        row = conn.execute("SELECT parent_id FROM session_v2 WHERE id = ?", (current,)).fetchone()
        if row is None:
            break
        found, current = current, row[0]
    return found


TREE_SQL = """
WITH RECURSIVE tree(id) AS (
  SELECT id FROM session_v2 WHERE id = ?
  UNION
  SELECT s.id FROM session_v2 s JOIN tree t ON s.parent_id = t.id
)
SELECT s.id, s.time_created, s.directory, s.model FROM session_v2 s JOIN tree t ON s.id = t.id
"""
"""The root and every descendant. UNION, not UNION ALL: a cycle ends the walk."""

ROWS_SQL = (
    "SELECT id, time_created, time_updated, data FROM session_message "
    "WHERE session_id = ? AND type IN ('assistant', 'compaction') AND time_updated >= ? "
    "ORDER BY seq"
)


def model_ref(value) -> dict:
    """A `{id, providerID}` model reference, from a dict or its JSON text; {} if neither."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def stamp_of(ms) -> tuple:
    """Epoch milliseconds -> (minute, ISO stamp), both UTC — the shape the other two
    reporters read off their transcripts, so `wire_models` and the server see no
    difference."""
    at = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    iso = at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"
    return iso[:16], iso


def classes(tokens: dict, provider: str) -> dict:
    """
    OpenCode's stored `tokens` block -> the ledger's rate classes (traps 1 and 2).

    The key set matches the reporter of the CLI the row is filed under: five classes
    for `claude-cli`, three for `codex-cli`, whose cache write is input.
    """
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    write = num(cache.get("write"))
    counts = {
        "promptTokens": num(tokens.get("input")),
        "completionTokens": num(tokens.get("output")) + num(tokens.get("reasoning")),
        "cacheReadTokens": num(cache.get("read")),
    }
    if provider == "claude-cli":
        counts.update(cacheWrite5mTokens=write, cacheWrite1hTokens=0)
    else:
        counts["promptTokens"] += write
    return counts


# ------------------------------------------------------------------------------------
# Whose money: a subscription login, or a key billed per call (trap 6)
# ------------------------------------------------------------------------------------

API_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
"""The variable each provider's key can come from instead of a stored credential."""

PER_CALL = "billed per call, not subscription quota"


def subscriptions(conn: sqlite3.Connection, env_keys: dict) -> dict:
    """
    `{cli: reason}` for the two reported providers — "" when the spend is subscription
    quota and may be reported, else why it is not.

    Decided by the provider's ACTIVE credential in OpenCode's `credential` table, and
    only by its TYPE. 2.0.29 keeps the type inside the `value` JSON — the row's own
    `method_id` / `connector_id` columns are never written — next to the secret, so
    `type` and `methodID` are pulled out by SQLite's `json_extract` and nothing else
    of `value` is ever selected: the key or token never enters this process.

    The types 2.0.29 writes: `oauth` (a login; OpenAI's ChatGPT one has methodID
    `chatgpt-token-sharing`, migrated from the 1.x `chatgpt-browser`, and a Claude
    Pro/Max login imported from 1.x has `oauth`), `key` (an API key, including the
    1.x `api` and `wellknown` ones it imports) and `external` (another tool's
    credential, e.g. azure-cli). Only `oauth` is a subscription. Everything else —
    a key, no credential at all, a type this release does not know, a database too
    old for `json_extract` — is skipped: under-reporting is visible and can be
    backfilled once fixed, a pay-per-call session filed as quota cannot be told
    apart from real quota afterwards.

    An API key in the environment (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, in the
    environment OpenCode's service runs with) skips the provider even beside an OAuth
    credential: which of the two OpenCode used is not recorded, and the doubt
    resolves the safe way. Only the variable's PRESENCE is looked at.

    This is the auth of NOW, not of the moment each message was spent. Reports run
    at the end of every turn, so the two only part when the login changes between a
    session's spend and its report — a `--backfill`, a first report after a reset.
    """
    found: dict = {}
    try:
        rows = conn.execute(
            "SELECT integration_id, json_extract(value, '$.type'), json_extract(value, '$.methodID') "
            "FROM credential WHERE integration_id IN ('anthropic', 'openai') "
            "ORDER BY active DESC, time_updated DESC"
        ).fetchall()
    except sqlite3.Error as err:
        log(f"opencode: credential types unreadable ({err!r}) — reporting nothing")
        rows = []
    for integration, kind, method in rows:
        found.setdefault(integration, (str(kind or ""), str(method or "")))

    verdict = {}
    for pid, cli in PROVIDERS.items():
        kind, method = found.get(pid, ("", ""))
        if env_keys.get(pid):
            verdict[cli] = f"{PER_CALL} ({API_KEY_ENV[pid]} is set)"
        elif kind == "oauth":
            verdict[cli] = ""
        elif kind == "key":
            verdict[cli] = f"{PER_CALL} ({pid} signs in with an API key)"
        elif not kind:
            verdict[cli] = f"{PER_CALL} (no {pid} login stored)"
        else:
            verdict[cli] = f"{PER_CALL} ({pid} credential type {kind!r} is not a subscription login)"
    return verdict


def contribution(raw: str, created_ms: int, fallback: dict, auth: dict):
    """
    One message row -> `[provider, model, minute, stamp, counts]`, or `(None, why)`.

    Filed under the minute the message was CREATED, not completed: a creation stamp
    never moves, so a message read while it was still streaming lands in the same
    row when it is read again finished, and no earlier minute has to shrink.

    `fallback` is the session's current model, for a FAILED compaction — the one
    spend that records tokens without the model it ran on. A compaction runs on the
    session's model, so that is the right one and not a guess.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "unreadable"
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None, "no tokens"
    ref = model_ref(data.get("model")) or fallback
    provider = PROVIDERS.get(str(ref.get("providerID") or ""))
    model = str(ref.get("id") or "").strip()
    if not provider:
        return None, f"provider {ref.get('providerID')!r} is not reported"
    if auth.get(provider, PER_CALL):
        return None, auth.get(provider, PER_CALL)
    if not model:
        return None, "no model named"
    counts = classes(tokens, provider)
    if not any(counts.values()):
        return None, "spent nothing"
    when = (data.get("time") or {}).get("created") if isinstance(data.get("time"), dict) else None
    minute, stamp = stamp_of(when if isinstance(when, int) and when > 0 else created_ms)
    return [provider, model, minute, stamp, counts], ""


# ------------------------------------------------------------------------------------
# Incremental reading: per-message contributions, keyed by id
# ------------------------------------------------------------------------------------

STATE_VERSION = 1


def new_state() -> dict:
    """
    `marks` is the newest `time_updated` read per session of the tree; `msgs` every
    counted message's own contribution, by message id; `sent` the totals last sent
    per (provider, model, minute).

    Contributions are kept rather than a running total because a row is rewritten in
    place (trap 4): its new reading has to REPLACE the old one. Keeping them also
    means a message OpenCode later deletes — a revert — stays counted. The quota was
    spent either way; a fresh re-read after losing this state would forget it, which
    under-reports, the safe direction.
    """
    return {"version": STATE_VERSION, "marks": {}, "msgs": {}, "sent": {}}


def load_state(raw):
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return None
    try:
        return {
            "version": STATE_VERSION,
            "marks": {str(k): int(v) for k, v in raw["marks"].items()},
            "msgs": {str(k): list(v) for k, v in raw["msgs"].items()},
            "sent": dict(raw["sent"]),
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def advance(conn: sqlite3.Connection, state: dict, root: str, auth: dict) -> str:
    """
    Fold every row of the tree written or rewritten since the last read into `state`.
    Returns the root session's directory. `auth` is `subscriptions`' verdict.

    A message skipped for its provider's auth is not looked at again unless OpenCode
    rewrites it, so a login that changes later neither adds old key-billed spend nor
    removes spend already counted under a subscription.

    `>=` the mark, not `>`: two writes can share a millisecond, and re-reading the
    newest row is free — it replaces its own contribution with the same one.
    """
    directory = ""
    skipped: dict = {}
    for session, born, where, current in conn.execute(TREE_SQL, (root,)).fetchall():
        if session == root:
            directory = str(where or "")
        fallback = model_ref(current)
        mark = state["marks"].get(session, 0)
        newest = mark
        for message, created, updated, raw in conn.execute(ROWS_SQL, (session, mark)).fetchall():
            newest = max(newest, updated)
            if created < born:
                why = "inherited from the session it was forked from"  # trap 3
            else:
                entry, why = contribution(raw, created, fallback, auth)
                if entry:
                    state["msgs"][message] = entry
                    continue
            skipped[why] = skipped.get(why, 0) + 1
        state["marks"][session] = newest
    for why, count in skipped.items():
        log(f"opencode {root}: skipped {count} message(s) — {why}")
    return directory


def tallies(state: dict) -> dict:
    """`{provider: {(model, minute): totals}}` — rebuilt from the contributions every
    time, which is exact by construction and cheap next to reading them."""
    out: dict = {}
    for provider, model, minute, stamp, counts in state["msgs"].values():
        entry = out.setdefault(provider, {}).setdefault((model, minute), dict.fromkeys(counts, 0) | {"calls": 0})
        for field, value in counts.items():
            entry[field] = entry.get(field, 0) + value
        entry["calls"] += 1
        entry["startedAt"] = min(entry.get("startedAt") or stamp, stamp)
        entry["endedAt"] = max(entry.get("endedAt") or stamp, stamp)
    return out


def _sent_key(provider: str, key: tuple) -> str:
    return f"{provider}\x1f{key[0]}\x1f{key[1]}"


# ------------------------------------------------------------------------------------
# What a report names
# ------------------------------------------------------------------------------------

def account_of(conn: sqlite3.Connection) -> str:
    """
    The OpenCode login's e-mail, from the `account` table — that one COLUMN and
    nothing else. The same table holds the login's access and refresh tokens, and
    `credential` holds the provider keys; neither is selected, so neither can reach a
    report or a log line. The active account when one is marked, else the only one;
    with several and none marked, nothing — a guessed attribution is a wrong one.
    """
    try:
        row = conn.execute(
            "SELECT a.email FROM account_state s JOIN account a ON a.id = s.active_account_id "
            "WHERE s.active_account_id IS NOT NULL LIMIT 1"
        ).fetchone()
        if not row:
            rows = conn.execute("SELECT email FROM account LIMIT 2").fetchall()
            row = rows[0] if len(rows) == 1 else None
    except sqlite3.Error:
        return ""
    return str(row[0] or "").strip() if row else ""


JSONC_RE = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/|,(?=\s*[}\]])', re.DOTALL)


def read_jsonc(path: Path) -> dict:
    """OpenCode's config is JSONC. Comments and trailing commas go; strings stay whole
    (a URL's `//` is inside one)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    plain = JSONC_RE.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    try:
        loaded = json.loads(plain)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def config_base_url(provider_id: str) -> str:
    """A base URL the global OpenCode config points this provider at, or "". Read from
    2.x's `providers.<id>.settings` and 1.x's `provider.<id>.options` alike."""
    directory = CONFIG_PATH.parent
    for name in ("opencode.jsonc", "opencode.json", "config.json"):
        config = read_jsonc(directory / name)
        for section, inner in (("providers", "settings"), ("provider", "options")):
            entry = (config.get(section) or {}).get(provider_id) if isinstance(config.get(section), dict) else None
            block = entry.get(inner) if isinstance(entry, dict) else None
            if isinstance(block, dict):
                for key in ("baseURL", "baseUrl", "endpoint"):
                    value = str(block.get(key) or "").strip()
                    if value:
                        return value
    return ""


def backend_of(provider_id: str) -> tuple:
    """
    WHICH ENDPOINT served the session, as (name, base url) — `firstParty` unless the
    environment or the OpenCode config points the provider somewhere else, in which
    case the server gets the sanitised URL to CHECK rather than a name to trust (see
    report_usage.backend_of). A project-level config can point a provider elsewhere
    too and is not read: the server refuses a custom backend it cannot verify, and a
    session it refuses is visible, where one weighed on the wrong tariff is not.
    """
    raw = os.environ.get(BASE_URL_ENV.get(provider_id, ""), "").strip() or config_base_url(provider_id)
    return ("custom", safe_url(raw)) if raw else ("firstParty", "")


def hook_context() -> dict:
    """The environment-derived fields, read in the HOOK's process — which OpenCode's
    service spawned, so its environment is the one the session ran under. Of the API
    key variables, only whether each is SET."""
    return {
        "backend": {cli: list(backend_of(pid)) for pid, cli in PROVIDERS.items()},
        "envKeys": {pid: bool(os.environ.get(var, "").strip()) for pid, var in API_KEY_ENV.items()},
    }


def project_from(directory: str) -> str:
    """The repo the session ran in — the Codex reporter's rule: `owner/repo` from the
    checkout when it is still there, the home-relative path when it is not."""
    if not directory:
        return ""
    return project_of(directory) if Path(directory).is_dir() else path_label(directory)


def body_of(config: dict, session: str, provider: str, models: dict, directory: str, account: str,
            context: dict | None = None, cap: bool = True) -> dict:
    """The wire shape — built in ONE place so `--print` cannot drift from what is sent."""
    context = context or hook_context()
    backend = tuple((context.get("backend") or {}).get(provider) or ("firstParty", ""))
    return {
        "sessionId": session,
        "provider": provider,
        "accountId": account,
        "project": project_from(directory),
        "device": device_of(config),
        "backend": backend[0],
        "baseUrl": backend[1],
        "bucketSize": "minute",
        "models": wire_models(models, cap),
    }


# ------------------------------------------------------------------------------------
# Reporting
# ------------------------------------------------------------------------------------

OUTCOME_RANK = {"idle": 0, "ok": 1, "drop": 2, "retry": 3}


def report_session(config: dict, session_id: str, context: dict | None = None,
                   root: Path | None = None, db: Path | None = None) -> str:
    """
    Report one session tree INCREMENTALLY and return the outcome ("idle" when nothing
    changed). One body per provider — a session that switched from Claude to GPT
    spent two quotas — each carrying only the minutes whose total changed, as TOTALS,
    so the server's upsert converges exactly as it does for the other two reporters.

    The database is closed before anything is posted: a read transaction left open
    across a slow network would hold back OpenCode's WAL checkpoint.
    """
    root = root or state_root() / "opencode"
    db = db or db_path()
    try:
        with closing(open_db(db)) as conn:
            top = root_of(conn, session_id)
            if not top:
                log(f"opencode {session_id}: no such session in {db}")
                return "idle"
            state_path = root / "state" / f"{_key_of(f'{db.resolve()}:{top}')}.json"
            state = load_state(read_json(state_path)) or new_state()
            auth = subscriptions(conn, (context or hook_context()).get("envKeys") or {})
            directory = advance(conn, state, top, auth)
            account = account_of(conn)
    except sqlite3.Error as err:
        log(f"opencode {session_id}: database unreadable: {err!r}")
        return "idle"

    outcome = "idle"
    for provider, per_key in sorted(tallies(state).items()):
        changed = {k: v for k, v in per_key.items() if state["sent"].get(_sent_key(provider, k)) != v}
        if not changed:
            continue
        body = body_of(config, top, provider, changed, directory, account, context, cap=False)
        # Spooled per (session, provider): a spooled body is merged with the next one
        # for the same key, and two providers' entries in one body would be filed
        # under whichever provider the newer one named.
        result = deliver(config, body, root / "spool", USER_AGENT, spool_key=f"{top}:{provider}")
        outcome = max(outcome, result, key=OUTCOME_RANK.get)
        for key, totals in changed.items():
            state["sent"][_sent_key(provider, key)] = totals
    write_json(state_path, state)
    return outcome


def print_session(config: dict, session_id: str, db: Path | None = None) -> None:
    """`--print`: a full, stateless read of what the session totals to right now."""
    db = db or db_path()
    with closing(open_db(db)) as conn:
        top = root_of(conn, session_id)
        if not top:
            print(f"no session {session_id} in {db}", file=sys.stderr)
            return
        state = new_state()
        directory = advance(conn, state, top, subscriptions(conn, hook_context()["envKeys"]))
        account = account_of(conn)
    for provider, per_key in sorted(tallies(state).items()):
        print(json.dumps(body_of(config, top, provider, per_key, directory, account), indent=2))


def sessions_of(argv: list, db: Path | None = None) -> list:
    """
    The session(s) a run is about: `--session ID`, or `--backfill [days]` — every
    session tree with a row touched in that window, by its ROOT, so a subagent that
    ran yesterday under an older parent still reaches the report it belongs to. With
    neither (only `--print` does that), the most recently active root session.
    """
    if "--session" in argv:
        index = argv.index("--session")
        return [argv[index + 1]] if len(argv) > index + 1 else []
    db = db or db_path()
    try:
        with closing(open_db(db)) as conn:
            if "--backfill" not in argv:
                row = conn.execute("SELECT id FROM session_v2 WHERE parent_id IS NULL "
                                   "ORDER BY time_updated DESC LIMIT 1").fetchone()
                return [row[0]] if row else []
            index = argv.index("--backfill")
            days = 1.0
            if len(argv) > index + 1 and not argv[index + 1].startswith("-"):
                try:
                    days = float(argv[index + 1])
                except ValueError:
                    pass
            cutoff = int((__import__("time").time() - days * 86400) * 1000)
            touched = conn.execute("SELECT id FROM session_v2 WHERE time_updated >= ? "
                                   "ORDER BY time_updated", (cutoff,)).fetchall()
            roots = [root_of(conn, row[0]) for row in touched]
    except sqlite3.Error as err:
        log(f"opencode: database unreadable: {err!r}")
        return []
    return list(dict.fromkeys(r for r in roots if r))


def session_of_payload(payload: dict, argv: list) -> str:
    """The hook passes `--session ID`; a JSON payload on stdin works too, for symmetry
    with the other reporters and for running it by hand."""
    if "--session" in argv:
        index = argv.index("--session")
        return argv[index + 1].strip() if len(argv) > index + 1 else ""
    return str(payload.get("session_id") or payload.get("sessionId") or payload.get("sessionID") or "").strip()


def main() -> int:
    argv = sys.argv[1:]
    if "--set-device" in argv:
        return device_command(argv, CONFIG_PATH)
    root = state_root() / "opencode"
    config = load_config(CONFIG_PATH)

    if "--print" in argv:
        for session in sessions_of(argv):
            print_session(config, session)
        return 0

    # stdin only when nothing else names the session: the plugin passes `--session`
    # with stdin closed, but a caller whose stdin is an open pipe would wait forever.
    reads_stdin = not any(flag in argv for flag in ("--worker", "--backfill", "--session"))
    payload = read_payload() if reads_stdin else {}
    if not configured(config):
        return 0

    def handle(queued: dict, context: dict) -> None:
        session = session_of_payload(queued, [])
        if session:
            report_session(config, session, context, root)

    if "--worker" in argv:
        return run_worker(root, config, handle, USER_AGENT)

    if "--backfill" in argv:
        lock = WorkerLock(root, blocking=True)
        lock.acquire()
        try:
            context = hook_context()
            for session in sessions_of(argv):
                report_session(config, session, context, root)
            flush_spool(config, root / "spool", USER_AGENT)
        finally:
            lock.release()
        return 0

    session = session_of_payload(payload, argv)
    if not session:
        log("opencode: no session id in the hook call — nothing to read")
        return 0
    return hook_entry(root, Path(__file__).resolve(), {"session_id": session}, hook_context(),
                      config, handle, USER_AGENT)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # never take a session down with us
        log(f"crashed: {err!r}")
        sys.exit(0)
