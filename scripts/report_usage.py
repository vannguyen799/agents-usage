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
    Cumulative totals per model for ONE session, SPLIT BY RATE CLASS (uncached input,
    cache read, 5-minute cache write, 1-hour cache write, output) and the name of the
    backend that served them, upserted on
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
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from functools import lru_cache
from pathlib import Path

VERSION = "0.7.0"
"""The reporter's version, and the only thing that says WHICH copy called.

It sat at 0.1.0 through two releases, so the one place a server could tell an old
reporter from a new one named a version that had not run in months -- which mattered
the moment the wire shape changed and the server had to handle both. CI now fails a
release where this disagrees with plugin.json, so it cannot drift again.

The CODEX reporter imports it from here rather than keeping its own: two copies of a
version string is two chances to ship one of them stale."""

USER_AGENT = f"agents-usage/{VERSION} (claude-code-hook)"
"""Sent on every report. urllib's default names Python and gets 403'd by a WAF."""

TIMEOUT_S = 5
"""Only ever called from a hook, so the session waits on this — keep it short."""

CONFIG_PATHS = (
    Path.home() / ".claude" / "agents-usage.json",
    Path.home() / ".codex" / "agents-usage.json",
)
"""Where the url and token live — ONE file serves both reporters, because the
credential belongs to the platform and not to a CLI. This is the fallback ORDER; each
reporter puts its own CLI's directory in front of it (see `config_paths`), and
`AGENTS_USAGE_CONFIG` in front of that. `~/.claude` leads the fallback because it is
where every copy installed so far already wrote one."""

DEFAULT_ENTRYPOINTS = ("cli",)
"""Transcript origins worth reporting — see trap 2. Widen with AGENTS_USAGE_ENTRYPOINTS."""


def log(message: str) -> None:
    """Diagnostics, off unless AGENTS_USAGE_DEBUG=1 — a hook must not print noise."""
    if os.environ.get("AGENTS_USAGE_DEBUG") != "1":
        return
    try:
        with log_path().open("a", encoding="utf-8") as fh:
            fh.write(f"{message}\n")
    except OSError:
        pass


def log_path() -> Path:
    """Beside the config, so a Codex-only machine does not log into a `~/.claude` it
    has no other reason to own. Unchanged where Claude Code is installed."""
    for candidate in config_paths():
        if candidate.parent.is_dir():
            return candidate.parent / "agents-usage.log"
    return Path.home() / "agents-usage.log"


def config_paths(preferred=None) -> tuple:
    """
    The candidates in order: `AGENTS_USAGE_CONFIG`, then the caller's OWN directory,
    then the rest. Each reporter puts its own CLI's directory first so that installing
    Codex on a machine that already carries a stale `~/.claude/agents-usage.json`
    points at the token just written rather than silently at the old one. Repeats in
    the list are harmless — the first readable file wins.
    """
    named = os.environ.get("AGENTS_USAGE_CONFIG", "").strip()
    ordered = (Path(named),) if named else ()
    if preferred:
        ordered += (Path(preferred),)
    return ordered + CONFIG_PATHS


def load_config(preferred=None) -> dict:
    """Env wins over the file, so one session can be pointed elsewhere for a test."""
    config = {}
    for path in config_paths(preferred):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            config = loaded
            break

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


REMOTE_SCHEMES = ("ssh", "git", "http", "https")
"""Remote URL schemes that name a SERVER. `file://` and the rest address a disk."""

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
"""What a segment of a repo slug may contain — notably neither `@` nor `:`."""


@lru_cache(maxsize=256)
def project_of(cwd: str) -> str:
    """
    Which REPO the session ran in, as `owner/repo` read from its `origin` remote.

    The remote and not the directory, because the remote is the repo's REAL name: one
    project reads identically on every machine and in every checkout, however the
    directory happened to be named, wherever it was cloned, and whether the session
    started at the root or five levels down.

    It also settles by itself the two cases the path could only guess at. A linked
    WORKTREE and a SUBMODULE each carry their own `origin`, so a branch parked in /tmp
    folds into its repo and a submodule gets its own line — with no path arithmetic and
    no `worktree list`, which inside a submodule answers with the SUPERPROJECT'S
    `.git/modules/…` gitdir and duly filed those sessions under exactly that.

    Falls back to `path_label` when there is no remote to read: a repo that has none, a
    directory that is not a repo at all, `safe.directory` refusing a checkout owned by
    someone else. A vaguer name is recoverable; a wrong one is not.

    Asked once per directory — memoised, because `--backfill` walks hundreds of
    transcripts sharing a handful of repos — with a short timeout.
    """
    if not cwd:
        return ""
    return remote_slug(cwd) or path_label(cwd)


def remote_slug(cwd: str) -> str:
    """`owner/repo` from the `origin` remote, or "" when it cannot be read as one."""
    try:
        done = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""  # no git, not a repo, or too slow
    return slug_of(done.stdout.strip()) if done.returncode == 0 else ""


def slug_of(url: str) -> str:
    """
    The PATH of a git remote URL as `owner/repo`; "" when the URL does not name one.

    A REMOTE URL IS A CREDENTIAL CARRIER. `https://x-access-token:ghp_…@github.com/o/r`
    is an ordinary thing to find in a checkout that a credential helper or a CI job
    wrote, and `project` is sent to the server and rendered on /usage — so this keeps
    the url's PATH and discards its AUTHORITY whole. A secret lives in the authority's
    userinfo, so dropping the authority drops the secret BY CONSTRUCTION, rather than by
    a pattern that would first have to recognise one. What survives is not trusted
    either: every segment must match SAFE_SEGMENT, which admits neither `@` nor `:`, so
    an authority that somehow slipped through cannot become a label.
    """
    scheme, sep, rest = url.partition("://")
    if sep:
        if scheme.lower() not in REMOTE_SCHEMES:
            return ""              # file:// and friends address a disk, not a forge
        path = rest.partition("/")[2]      # a `host:port` authority keeps its own colon
    elif "@" in url.partition(":")[0]:
        # scp-like `git@host:owner/repo.git` — no scheme, and the authority is
        # everything up to the FIRST colon.
        path = url.partition(":")[2]
    else:
        return ""                  # a bare local path — nothing here names a repo

    if path.endswith(".git"):
        path = path[:-len(".git")]
    segments = [s for s in path.strip("/").split("/") if s]
    # `owner/repo` at the least; a GitLab subgroup legitimately adds more.
    if len(segments) < 2 or not all(SAFE_SEGMENT.match(s) for s in segments):
        return ""
    return "/".join(segments)


def path_label(cwd: str) -> str:
    """
    The fallback name: the checkout's path relative to this machine's home.

    Relative on two counts: it keeps the username out of the value, and it makes one
    repo checked out on two machines read as ONE project rather than two unrelated
    absolute paths.

    Resolved through git rather than by taking the last path segment, because sessions
    are routinely started inside a subdirectory — `acme/frontend/apps/storefront` would
    otherwise be filed under `storefront`, which names nothing. `worktree list` and not
    `rev-parse --show-toplevel`, because the toplevel of a LINKED WORKTREE is the
    worktree: a branch parked in /tmp would open a second line for a repo that already
    has one, and — being outside home — would report an absolute path with the username
    in it, which is the one thing relativising was for. The first entry `worktree list`
    prints is the main checkout, from anywhere in the family.
    """
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


MAX_DEVICE = 64
"""Longest label sent. The server caps it too; this keeps a pathological hostname from
making the payload strange in the first place."""


def user_host() -> str:
    """
    This machine as its own shell names it: `user@host`.

    The SHORT hostname, not the FQDN — `dev-pc` is what a human calls the box, and the
    domain that follows only records which network it was on when it reported. Either
    half is dropped when the box will not say (a container with no passwd entry, a host
    with no name): half a label still names something, and an empty one names nothing.
    """
    try:
        user = getpass.getuser().strip()
    except Exception:  # no passwd entry AND no USER in the environment
        user = ""
    try:
        host = socket.gethostname().strip().split(".")[0]
    except OSError:
        host = ""
    if user and host:
        return f"{user}@{host}"
    return host or user


def machine_id() -> str:
    """
    Claude Code's own `machineID` from ~/.claude.json — stable per machine and opaque.

    No longer the default, and no longer OURS to depend on: it named a device only to
    whoever could look the id up, and only on a machine where Claude Code is installed.
    It survives as `device_id`'s last resort, for the box that can name neither its user
    nor its host AND cannot persist an id of its own.
    """
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


def device_of(config: dict) -> str:
    """
    Which MACHINE reported, so quota can be split per device.

    A LABEL only: multi-device accounting is already correct without it, because the
    upsert key carries a session UUID that no two machines can collide on. What it
    decides is whether the person reading /usage can tell which box a row came from.

    In order: the name this machine was GIVEN (`--set-device`, `device` in the config,
    or `AGENTS_USAGE_DEVICE`), then `user@host`, then this machine's own `device_id`
    for a box that will name neither. BOTH reporters call this one function, so one
    laptop stays one row on the breakdown whichever CLI spent the quota.

    `user@host` is the default deliberately, in place of the machineID this used to
    send: it carries this machine's login and hostname, which is the point — a fleet's
    rows are read by the person who owns the fleet, and `dev-pc` is worth more to them
    than 12 hex characters. Name the machine to send something else instead.
    """
    named = str(config.get("device") or "").strip()
    if named:
        return named[:MAX_DEVICE]
    return (user_host() or device_id())[:MAX_DEVICE]


def device_id() -> str:
    """
    This machine's own id — OURS, not a harness's, and the DEVICE's, not a login's.

    Every CLI already keeps an id for its own machine (Claude Code's `machineID`,
    Codex's `installation_id`), and each names the same laptop differently, so a
    reporter that borrowed one would name the box differently per harness — and would
    have nothing at all to read on a harness that keeps none.

    It follows the DEVICE on purpose. Who spent the quota is already a field of its
    own (`accountId`), so the only question this one answers is *which box*, and an id
    that changed per login would answer a question nothing asked while splitting one
    laptop across the breakdown. Two people sharing a workstation report the same
    device and stay apart by their account; the default `user@host` label is the view
    that separates them by hand.

    Resolved in three ways, in order:

    1. An id already stored in a config, scanned in a FIXED order (`id_paths`, never
       the caller's preferred-first order) — two reporters resolving the same set of
       files must land on the same id, or the machine splits in two. This comes first
       so a box that has already reported keeps the identity it reported under.
    2. DERIVED from the operating system's own machine id — `/etc/machine-id`, macOS's
       IOPlatformUUID, Windows' MachineGuid. That is what survives the two things a
       stored id does not: the config file being deleted (the next install re-derives
       the SAME id rather than inventing a new device) and a second OS user on the box
       (who derives it too, instead of appearing as another machine).
    3. A random id, persisted. Only for a box whose OS will not name itself.

    The OS id is HASHED with an app-specific salt, never sent raw: it is a stable
    identifier for the whole machine, systemd documents that applications must derive
    from it rather than expose it, and a value we would not want a server to see is a
    value we should not put in a payload.

    A RANDOM id that cannot be persisted is worse than none — every session would
    generate its own and each would read as a separate device — so that path falls
    back to the harness id instead. A derived one needs no file to stay stable, which
    is exactly why it is preferred; it is still stored, best-effort, so an OS that
    later changes its own id (a re-image, a cloned VM) does not rename the box.
    """
    for path in id_paths():
        try:
            value = str(json.loads(path.read_text(encoding="utf-8")).get("deviceId") or "").strip()
        except (OSError, ValueError):
            continue
        if value:
            return value[:MAX_DEVICE]

    seed = machine_seed()
    if seed:
        derived = hashlib.sha256(f"agents-usage:{seed}".encode("utf-8")).hexdigest()[:12]
        update_config({"deviceId": derived})
        return derived

    fresh = uuid.uuid4().hex[:12]
    return fresh if update_config({"deviceId": fresh}) else machine_id()


MACHINE_ID_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")
"""Linux's own id for the box. The dbus copy is the fallback on systems that predate
systemd or keep only that one; both hold the same value where both exist."""


def machine_seed() -> str:
    """
    What the OPERATING SYSTEM calls this machine — raw, and never sent anywhere: it is
    hashed before it becomes an id (see `device_id`).

    One source per platform, cheapest first. Linux and Windows answer from a file and
    the registry; macOS keeps its IOPlatformUUID nowhere a file can reach it, so that
    one costs a subprocess — which is affordable HERE and nowhere else in this script,
    because the result is written to the config on the first call and every later
    session reads it back instead.

    Returns "" for a box that will not say, which is a normal answer: a container has
    no stable identity of its own, and inventing one from its hostname would give
    every rebuild of it a new device.
    """
    for path in MACHINE_ID_FILES:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value

    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=3,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        found = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
        if found:
            return found.group(1)

    if os.name == "nt":
        try:
            import winreg  # noqa: PLC0415  (Windows-only, and only on this path)
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
        except (ImportError, OSError):
            pass

    return ""


def id_paths() -> tuple:
    """Where a `deviceId` may live, in the order every reporter must agree on."""
    named = os.environ.get("AGENTS_USAGE_CONFIG", "").strip()
    return ((Path(named),) if named else ()) + CONFIG_PATHS


def config_target(preferred=None) -> Path:
    """
    Which config file a WRITE lands in: `AGENTS_USAGE_CONFIG` when it names one at all
    (an explicit path is a decision, whether or not the file exists yet), else the
    first that already exists in the read order — so naming a machine changes the file
    the reporter actually reads — else the caller's own directory.
    """
    named = os.environ.get("AGENTS_USAGE_CONFIG", "").strip()
    if named:
        return Path(named)
    for candidate in config_paths(preferred):
        if candidate.is_file():
            return candidate
    return Path(preferred) if preferred else CONFIG_PATHS[0]


def update_config(changes: dict, preferred=None):
    """
    Merge `changes` into the config and write it back; a `None` value removes its key.
    Returns the path written, or None when the box would not take it.

    Read-modify-write, 0600, temp-and-rename — the same care the installer takes with
    this file, for the same two reasons: it holds the API token, so it must never exist
    for an instant in a mode the rest of the machine can read, and a write that dropped
    `url` or `token` would silently stop every report from this box.
    """
    target = config_target(preferred)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            config = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        for key, value in changes.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".agents-usage-")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump(config, fh, indent=2)
            fh.write("\n")
        os.replace(temporary, target)
    except OSError as err:
        log(f"config write failed: {err!r}")
        return None
    return target


def set_device(name: str, preferred=None) -> int:
    """
    Name this machine, or with an empty name give it back its derived one.

    `--id` asks for the opaque id instead of a name — the escape hatch for a box that
    must not report its login and hostname, and the one value that is stable across
    every harness on it.
    """
    name = name.strip()
    if name == "--id":
        name = device_id()
        if not name:
            print("no device id could be stored — is the config directory writable?", file=sys.stderr)
            return 1
    target = update_config({"device": name[:MAX_DEVICE] or None}, preferred)
    if target is None:
        print(f"could not write {config_target(preferred)}", file=sys.stderr)
        return 1
    print(f"device = {device_of(load_config(preferred)) or '(none)'}  [{target}]")
    override = os.environ.get("AGENTS_USAGE_DEVICE", "").strip()
    if override:
        print(f"note: AGENTS_USAGE_DEVICE={override} is set and wins over the file", file=sys.stderr)
    return 0


def device_command(argv: list, preferred=None) -> int:
    """
    `--set-device [NAME]`, shared by both reporters.

    With a name it names the machine; with `--id` it uses this machine's own opaque id
    instead; with an EMPTY name (`--set-device ''`) it clears the name and goes back to
    `user@host`; with no argument at all it only says what the label is now. That last
    case is the reason the argument is optional: a flag that could only ever write
    would clear the name of whoever typed it to look.
    """
    index = argv.index("--set-device")
    if len(argv) > index + 1:
        return set_device(argv[index + 1], preferred)
    print(f"device = {device_of(load_config(preferred)) or '(none)'}")
    print("--set-device NAME names it · --set-device --id uses the opaque id "
          "· --set-device '' goes back to user@host")
    return 0


REDIRECT_VARS = (
    ("CLAUDE_CODE_USE_BEDROCK", "bedrock"),
    ("CLAUDE_CODE_USE_VERTEX", "vertex"),
    ("CLAUDE_CODE_USE_FOUNDRY", "foundry"),
    ("CLAUDE_CODE_USE_GATEWAY", "gateway"),
    ("CLAUDE_CODE_USE_MANTLE", "mantle"),
    ("CLAUDE_CODE_USE_ANTHROPIC_AWS", "anthropicAws"),
    ("CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "anthropicGoogleCloud"),
)
"""Env flags that point Claude Code at a partner-operated backend. Names read out of
the shipped CLI binary rather than recalled."""

BASE_URL_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_HOST", "CLAUDE_CODE_API_BASE_URL")


def backend_of() -> tuple[str, str]:
    """
    WHICH ENDPOINT this session was talking to, as (name, base url).

    The transcript names none -- scanned across 4001 assistant records, no field does
    -- so it is read from the environment. That works here and nowhere else: this hook
    runs INSIDE the session it reports on, so its own environment is that session's.

    It matters because the server WEIGHTS these tokens on Anthropic's ratios, and the
    partner backends are separately priced. The server decides what to do with a
    session that was not served natively; this only has to report honestly enough for
    that decision to be possible, which is why the URL goes along with the name -- a
    name alone cannot be checked.

    THE URL IS SANITISED, not passed through. `https://user:ghp_x@host/v1?token=abc`
    is an ordinary thing to find in an environment, and this value is sent off the
    machine. Only scheme, host, port and path survive; the userinfo (where a secret
    lives) and the query and fragment (where the other one does) are dropped by
    construction rather than by a pattern that must first recognise a secret.
    """
    for var, name in REDIRECT_VARS:
        if os.environ.get(var, "").strip() not in ("", "0", "false", "False"):
            return name, ""
    for var in BASE_URL_VARS:
        raw = os.environ.get(var, "").strip()
        if raw:
            return "custom", safe_url(raw)
    return "firstParty", ""


def safe_url(raw: str) -> str:
    """Scheme, host, port and path of a URL -- never its userinfo, query or fragment."""
    try:
        u = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if not u.hostname:
        return ""
    host = u.hostname + (f":{u.port}" if u.port else "")
    return f"{u.scheme or 'https'}://{host}{u.path.rstrip('/')}"[:200]


def nested_transcripts(transcript: Path) -> list[Path]:
    """
    Every transcript written UNDER the session's own directory: the sub-sessions the Task
    tool spawned (`<session-id>/subagents/agent-*.jsonl`) and the agents a workflow ran
    (`<session-id>/subagents/workflows/wf_*/agent-*.jsonl`, two levels deeper again).

    Claude Code writes all of it beside the session rather than into it, and what those
    agents spent reaches the parent transcript nowhere: of 15,567 subagent responses
    written here over a month, exactly one shared a `message.id` with its parent. Reading
    only `<project>/<session>.jsonl` therefore drops that spend in silence — 2.70B tokens
    of 16.6B on this machine, 16% of the real total.

    Swept recursively rather than by naming the two layouts, because the directory is the
    contract: anything under a session id was spent BY that session, and a third nesting
    level would otherwise go missing exactly as quietly as the first two did. Non-transcript
    files that live there (a workflow's `journal.jsonl`) carry no assistant record with a
    `usage` block and fall out of the tally on their own.

    They fold into the session that spawned them rather than reporting as sessions of
    their own: the directory IS the parent's id, an agent id is not a session id the
    platform could resolve, and one row per session keeps the upsert key stable.
    """
    directory = transcript.parent / transcript.stem
    try:
        return sorted(p for p in directory.rglob("*.jsonl") if p.is_file())
    except OSError:
        return []


def tally(transcript: Path, entrypoints: set) -> tuple[dict, int, str]:
    """
    Cumulative tokens per model for one SESSION — its transcript plus every subagent
    transcript it spawned — and the number of records skipped because they did not come
    from an accepted entrypoint.

    Deduplicated on `message.id` across the whole set, not per file (see the module
    docstring), so a response that does reach both parent and subagent is still counted
    once.

    THE CACHE CLASSES ARE SENT APART, not folded into the prompt count. They used to be
    folded, and the result was a /usage page reporting 2.1 BILLION tokens for a month
    of work: 98.3% of them were cache reads, which count for a TENTH of an uncached
    token, while the writes count for MORE than one (1.25x at the 5-minute TTL, 2x at
    the 1-hour one Claude Code uses) and output counts for FIVE. One folded number
    cannot be taken back apart, so the weighting has to happen on counts that were
    never merged. The server weights them; this only has to keep them separate.

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
    for path in [transcript, *nested_transcripts(transcript)]:
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

                # The per-TTL split is what `cache_creation` carries, and it is read
                # rather than assumed: the two TTLs weigh differently (x2 against
                # x1.25) and both occur -- across 120 transcripts here, 95.7% of write
                # tokens are 1-hour and 9% of sessions write some 5-minute ones.
                # The flat `cache_creation_input_tokens` is the older shape and carries
                # no TTL at all; it falls to the 1-hour bucket, the larger case, since
                # guessing the cheaper one would under-count that term by 37.5%.
                created = usage.get("cache_creation")
                created = created if isinstance(created, dict) else {}
                write_5m = num(created.get("ephemeral_5m_input_tokens"))
                write_1h = num(created.get("ephemeral_1h_input_tokens"))
                if not write_5m and not write_1h:
                    write_1h = num(usage.get("cache_creation_input_tokens"))

                counts = {
                    "promptTokens": num(usage.get("input_tokens")),
                    "completionTokens": num(usage.get("output_tokens")),
                    "cacheReadTokens": num(usage.get("cache_read_input_tokens")),
                    "cacheWrite5mTokens": write_5m,
                    "cacheWrite1hTokens": write_1h,
                }
                if not any(counts.values()):
                    continue

                entry = per_model.setdefault(model, dict.fromkeys(counts, 0) | {"calls": 0})
                for key, value in counts.items():
                    entry[key] += value
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


def body_of(config: dict, session: str, models: dict, cwd: str) -> dict:
    """
    The wire shape of one report — built in ONE place so `--print` cannot drift from
    what is actually sent, which is the only way to check this without a server.

    `provider` names WHICH CLI spent the quota. A server older than 0.7.0 ignores the
    field and files the row as `claude-cli`, which is what every row from here has
    always been, so sending it costs an old deploy nothing.
    """
    backend = backend_of()
    return {
        "sessionId": session,
        "provider": "claude-cli",
        "accountId": claude_account_id(),
        "project": project_of(cwd),
        "device": device_of(config),
        "backend": backend[0],
        "baseUrl": backend[1],
        "models": [{"model": model, **totals} for model, totals in sorted(models.items())],
    }


def post(config: dict, body: dict, user_agent: str = USER_AGENT) -> bool:
    """
    POST one report to the ledger.

    Shared with the Codex reporter: the endpoint, the timeout, the failure handling
    and the User-Agent are properties of the PLATFORM, not of the CLI being reported
    on, and a second copy of them is a second place for the WAF lesson below to be
    forgotten.
    """
    session = body.get("sessionId")
    request = urllib.request.Request(
        f"{config['url']}/api/v1/usage/sessions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {config['token']}",
            # Named on purpose. urllib's default UA is `Python-urllib/3.x`, which a WAF
            # in front of the platform rejects outright (Cloudflare error 1010) — the
            # report came back 403 with nothing in it that looked like an auth problem.
            "user-agent": user_agent,
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
    if "--set-device" in argv:
        return device_command(argv)
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
        body = body_of(config, session_id_of(payload, transcript), models, cwd)
        if dry_run:
            print(json.dumps(body, indent=2))
            continue
        post(config, body)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # never take a session down with us
        log(f"crashed: {err!r}")
        sys.exit(0)
