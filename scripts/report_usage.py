#!/usr/bin/env python3
"""
Report a Claude Code session's token usage to the agent platform's usage ledger.

WHY THIS EXISTS
    An agent running on the platform's `claude-cli` provider is metered: the run
    writes ledger rows and /usage can say whose subscription quota it spent. A human
    typing into Claude Code on the same login spends the SAME quota and the platform
    sees none of it, so the account-level picture is missing most of the spend. This
    reports the other half in.

WHAT IT SENDS
    Cumulative totals per model for ONE session, upserted server-side on
    `cli:<sessionId>:<model>`. Cumulative, not incremental, is the whole design: hooks
    fire an unpredictable number of times, are retried, and can miss a turn entirely.
    Sending totals-so-far means the row converges regardless, and neither side has to
    keep a watermark that could be lost and then double-counted.

THE TWO TRAPS THIS AVOIDS
    1. ONE API response is written to the transcript as SEVERAL JSONL lines (one per
       content block), and every one of them repeats the full `usage` block. Summing
       the lines over-counts by 2-3x. Records are deduplicated on `message.id` before
       anything is added up — the count this sends is API responses, not lines.

    2. Only INTERACTIVE sessions (`entrypoint: "cli"`) are reported. Anything driven by
       the Agent SDK (`sdk-cli`) belongs to whatever program drove it, and that program
       is expected to meter itself — the agent platform does exactly that for every run
       on its `claude-cli` provider. Reporting those here would bill the same tokens
       twice: once from the platform's own ledger write, once from this transcript.
       The platform additionally passes `persistSession: false`, so its runs write no
       transcript at all today, but that is ITS choice to make and can be revisited;
       this filter is what makes the two safe to run on one host by construction rather
       than by coincidence. On this machine 1833 `sdk-cli` transcripts from an unrelated
       service show exactly what gets swept up without it.

Never fails a session: every error path exits 0 and stays quiet.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

USER_AGENT = "agents-usage/0.1.0 (claude-code-hook)"

TIMEOUT_S = 5
"""Only ever called from a hook, so the session waits on this — keep it short."""

CONFIG_PATH = Path.home() / ".claude" / "agents-usage.json"

DEFAULT_ENTRYPOINTS = ("cli",)
"""Transcript origins worth reporting — see trap 2. Widen with AGENTS_USAGE_ENTRYPOINTS."""


def log(message: str) -> None:
    """Diagnostics, off unless AGENTS_USAGE_DEBUG=1 — a hook must not print noise."""
    if os.environ.get("AGENTS_USAGE_DEBUG") != "1":
        return
    try:
        path = Path.home() / ".claude" / "agents-usage.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{message}\n")
    except OSError:
        pass


def load_config() -> dict:
    """Env wins over the file, so one session can be pointed elsewhere for a test."""
    config = {}
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        pass

    url = (os.environ.get("AGENTS_USAGE_URL") or config.get("url") or "").strip()
    token = (os.environ.get("AGENTS_USAGE_TOKEN") or config.get("token") or "").strip()
    enabled = os.environ.get("AGENTS_USAGE_ENABLED", str(config.get("enabled", "1")))

    raw = os.environ.get("AGENTS_USAGE_ENTRYPOINTS")
    listed = raw.split(",") if raw else config.get("entrypoints") or DEFAULT_ENTRYPOINTS
    entrypoints = {str(e).strip() for e in listed if str(e).strip()} or set(DEFAULT_ENTRYPOINTS)

    return {
        "url": url.rstrip("/"),
        "token": token,
        "enabled": enabled not in ("0", "false", "False"),
        "entrypoints": entrypoints,
        "device": os.environ.get("AGENTS_USAGE_DEVICE") or config.get("device") or "",
    }


def claude_account_id() -> str:
    """
    The login whose quota this session spent.

    Read from the CLI's own config rather than probed with `claude auth status`: this
    runs on every turn and must not spawn a subprocess. The server treats it as a
    label, so a stale or missing value costs the row its attribution and nothing more.
    """
    directory = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    candidates = [Path(directory) / ".claude.json"] if directory else []
    candidates.append(Path.home() / ".claude.json")
    for path in candidates:
        try:
            account = json.loads(path.read_text(encoding="utf-8")).get("oauthAccount") or {}
        except (OSError, ValueError):
            continue
        for key in ("emailAddress", "accountUuid"):
            value = str(account.get(key) or "").strip()
            if value:
                return value
    return ""


@lru_cache(maxsize=256)
def project_of(cwd: str) -> str:
    """
    Which REPO the session ran in, as a path relative to this machine's home.

    Relative on two counts: it keeps the username out of the value, and it makes one
    repo checked out on two machines read as ONE project rather than two unrelated
    absolute paths.

    Resolved through git rather than by taking the last path segment, because sessions
    are routinely started inside a subdirectory — `acme/frontend/apps/storefront`
    would otherwise be filed under `storefront`, which names nothing.

    `worktree list` and not `rev-parse --show-toplevel`, because the toplevel of a LINKED
    WORKTREE is the worktree: a branch parked in /tmp opens a second project line for a
    repo that already has one, and — being outside home — reports an absolute path with
    the username in it, which is the one thing relativising was for. The first entry
    `worktree list` prints is the main checkout, from anywhere in the family, so every
    worktree of a repo agrees on one label. A SUBMODULE deliberately does NOT fold into
    its superproject: it is its own repo with its own history, and filing its sessions
    under the parent would hide them.

    Asked once per directory — memoised, because `--backfill` walks hundreds of
    transcripts sharing a handful of repos — with a short timeout. Every failure (no git,
    not a repo, `safe.directory` refusing a checkout owned by someone else) falls back to
    the directory itself, which is still a usable label.
    """
    if not cwd:
        return ""
    root = cwd
    try:
        done = subprocess.run(
            ["git", "-C", cwd, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if done.returncode == 0:
            for line in done.stdout.splitlines():
                if line.startswith("worktree "):
                    root = line[len("worktree "):].strip() or root
                    break  # the main checkout is the first entry git prints
    except (OSError, subprocess.SubprocessError):
        pass  # no git, not a repo, or too slow — the directory is still a usable label

    # Resolved once, so a symlinked route into a repo cannot read as a second project.
    resolved = Path(root).resolve()
    try:
        relative = str(resolved.relative_to(Path.home().resolve()))
    except ValueError:
        return str(resolved)  # outside home — no username to strip anyway
    # `.` is what home itself relativises to, and it names nothing in a breakdown.
    return "~" if relative == "." else relative


def device_of(config: dict) -> str:
    """
    Which MACHINE reported, so quota can be split per device.

    A LABEL only: multi-device accounting is already correct without it, because the
    upsert key carries a session UUID that no two machines can collide on.

    Claude Code keeps its own `machineID` in ~/.claude.json — stable per machine and
    opaque, so it names a device without carrying its hostname. A `device` in the config
    wins, because a human reading the ledger wants "dev-pc", not 12 hex characters.
    """
    named = str(config.get("device") or "").strip()
    if named:
        return named
    directory = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    candidates = [Path(directory) / ".claude.json"] if directory else []
    candidates.append(Path.home() / ".claude.json")
    for path in candidates:
        try:
            machine = str(json.loads(path.read_text(encoding="utf-8")).get("machineID") or "")
        except (OSError, ValueError):
            continue
        if machine:
            return machine[:12]
    return ""


def subagent_transcripts(transcript: Path) -> list[Path]:
    """
    The sub-sessions the Task tool ran inside this one.

    Claude Code writes them BESIDE the session rather than into it — as
    `<project>/<session-id>/subagents/agent-*.jsonl` — and what they spent appears
    nowhere in the parent: of 15,567 subagent responses written here over a month,
    exactly one shared a `message.id` with a parent transcript. Reading only
    `<project>/<session>.jsonl` therefore silently drops every token a subagent spent —
    2.65B of 16.7B on this machine, 16% of the real total.

    They fold into the session that spawned them rather than reporting as sessions of
    their own: the directory IS the parent's id, an agent id is not a session id the
    platform could resolve, and one row per session keeps the upsert key stable.
    """
    directory = transcript.parent / transcript.stem / "subagents"
    try:
        return sorted(p for p in directory.glob("*.jsonl") if p.is_file())
    except OSError:
        return []


def tally(transcript: Path, entrypoints: set) -> tuple[dict, int, str]:
    """
    Cumulative tokens per model for one SESSION — its transcript plus every subagent
    transcript it spawned — and the number of records skipped because they did not come
    from an accepted entrypoint.

    Deduplicated on `message.id` across the whole set, not per file (see the module
    docstring), so a response that does reach both parent and subagent is still counted
    once. Cache reads and cache writes fold into the prompt count, which is what the
    platform's own providers do, so a token means the same thing on every row of the
    ledger.

    A record whose `entrypoint` is missing or unrecognised is SKIPPED, not reported.
    Under-reporting is visible in the numbers and recoverable; double-reporting is
    silent and corrupts the per-account view, so the doubt resolves the safe way.
    """
    per_model: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    skipped = 0
    cwd = ""

    # Parent first: it is the session's own record of where it ran, and a subagent
    # inherits that directory rather than defining it.
    for path in [transcript, *subagent_transcripts(transcript)]:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue

        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a partially written line while the session is live
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                if str(record.get("entrypoint") or "") not in entrypoints:
                    skipped += 1
                    continue
                cwd = cwd or str(record.get("cwd") or "")

                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue

                # One API response, many transcript lines — count it once.
                key = message.get("id") or record.get("requestId") or record.get("uuid")
                if not key or key in seen:
                    continue
                seen.add(key)

                model = str(message.get("model") or "").strip()
                # `<synthetic>` is Claude Code speaking for itself (an API error rendered
                # as an assistant turn); no model ran and no quota was spent.
                if not model or model.startswith("<"):
                    continue

                prompt = (
                    num(usage.get("input_tokens"))
                    + num(usage.get("cache_read_input_tokens"))
                    + num(usage.get("cache_creation_input_tokens"))
                )
                completion = num(usage.get("output_tokens"))
                if prompt <= 0 and completion <= 0:
                    continue

                entry = per_model.setdefault(model, {"promptTokens": 0, "completionTokens": 0, "calls": 0})
                entry["promptTokens"] += prompt
                entry["completionTokens"] += completion
                entry["calls"] += 1

    return per_model, skipped, cwd


def num(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def session_id_of(payload: dict, transcript: Path) -> str:
    """The hook names it; the transcript filename is the fallback (it IS the id)."""
    for key in ("session_id", "sessionId"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return transcript.stem


def report(config: dict, session: str, models: dict, cwd: str) -> bool:
    body = json.dumps({
        "sessionId": session,
        "accountId": claude_account_id(),
        "project": project_of(cwd),
        "device": device_of(config),
        "models": [{"model": model, **totals} for model, totals in sorted(models.items())],
    }).encode("utf-8")

    request = urllib.request.Request(
        f"{config['url']}/api/v1/usage/sessions",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {config['token']}",
            # Named on purpose. urllib's default UA is `Python-urllib/3.x`, which a WAF
            # in front of the platform rejects outright (Cloudflare error 1010) — the
            # report came back 403 with nothing in it that looked like an auth problem.
            "user-agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            log(f"reported {session}: {response.status} {response.read(200)!r}")
            return True
    except urllib.error.HTTPError as err:
        log(f"reported {session}: HTTP {err.code} {err.read(200)!r}")
    except (urllib.error.URLError, OSError, ValueError) as err:
        log(f"reported {session}: {err}")
    return False


def transcripts_of(payload: dict, argv: list[str]) -> list[Path]:
    """
    Normally exactly the transcript the hook named.

    `--backfill [days]` instead sweeps every transcript touched recently, which is how
    a session that died without a final hook gets counted: the report is cumulative and
    the server upserts, so re-reporting a session that was already reported is a no-op.
    """
    if "--backfill" in argv:
        index = argv.index("--backfill")
        days = float(argv[index + 1]) if len(argv) > index + 1 and not argv[index + 1].startswith("-") else 1.0
        cutoff = __import__("time").time() - days * 86400
        root = Path.home() / ".claude" / "projects"
        return sorted(
            (p for p in root.glob("*/*.jsonl") if p.stat().st_mtime >= cutoff),
            key=lambda p: p.stat().st_mtime,
        )

    path = str(payload.get("transcript_path") or payload.get("transcriptPath") or "").strip()
    return [Path(path)] if path else []


def main() -> int:
    argv = sys.argv[1:]
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

    config = load_config()
    if not dry_run and not (config["enabled"] and config["url"] and config["token"]):
        log("skipped: not configured (need url + token, and enabled)")
        return 0

    for transcript in transcripts_of(payload, argv):
        if not transcript.is_file():
            continue
        models, skipped, cwd = tally(transcript, config["entrypoints"])
        if skipped:
            # Never silent: a sweep that quietly dropped most of its input reads as
            # "there was nothing to report", which is the wrong conclusion entirely.
            log(f"{transcript.name}: skipped {skipped} record(s) from another entrypoint")
        if not models:
            continue
        session = session_id_of(payload, transcript)
        if dry_run:
            print(json.dumps({
                "sessionId": session,
                "accountId": claude_account_id(),
                "project": project_of(cwd),
                "device": device_of(config),
                "models": [{"model": m, **t} for m, t in sorted(models.items())],
            }, indent=2))
            continue
        report(config, session, models, cwd)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # never take a session down with us
        log(f"crashed: {err!r}")
        sys.exit(0)
