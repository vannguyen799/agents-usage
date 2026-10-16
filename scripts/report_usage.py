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
    `cli:<sessionId>:<model>:<minute>`. Cumulative, not incremental, is the whole design: hooks
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

VERSION = "0.12.0"
"""The reporter's version, and the only thing that says WHICH copy called.

It sat at 0.1.0 through two releases, so the one place a server could tell an old
reporter from a new one named a version that had not run in months -- which mattered
the moment the wire shape changed and the server had to handle both. CI now fails a
release where this disagrees with plugin.json, so it cannot drift again.

The CODEX and OPENCODE reporters import it from here rather than keeping their own:
two copies of a version string is two chances to ship one of them stale."""

USER_AGENT = f"agents-usage/{VERSION} (claude-code-hook)"
"""Sent on every report. urllib's default names Python and gets 403'd by a WAF."""

TIMEOUT_S = 5
"""Only ever called from a hook, so the session waits on this — keep it short."""

CONFIG_PATHS = (
    Path.home() / ".claude" / "agents-usage.json",
    Path.home() / ".codex" / "agents-usage.json",
    (Path(os.environ.get("XDG_CONFIG_HOME", "").strip() or Path.home() / ".config")
     / "opencode" / "agents-usage.json"),
)
"""Where the url and token live — ONE file serves every reporter, because the
credential belongs to the platform and not to a CLI. This is the fallback ORDER; each
reporter puts its own CLI's directory in front of it (see `config_paths`), and
`AGENTS_USAGE_CONFIG` in front of that. `~/.claude` leads the fallback because it is
where every copy installed so far already wrote one; OpenCode's comes last, so a
machine that only ever installed OpenCode is still found by the other two."""

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
        # Kept raw: the Codex reporter parses it (`originators()`), and dropping it here
        # silently ignored the config file's list.
        "originators": config.get("originators") or [],
        # Kept raw for the Codex reporter: where a LiteLLM proxy's config lives.
        "litellmConfig": config.get("litellmConfig") or "",
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


# ------------------------------------------------------------------------------------
# Billing: quota, or money (0.12.0)
# ------------------------------------------------------------------------------------

OFFICIAL_HOSTS = {"anthropic": "api.anthropic.com", "openai": "api.openai.com"}
"""The vendors' own API hosts — the same list the server checks a `custom` backend
against before it will price a per-call session on that vendor's rates."""

API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
"""Where Claude Code takes an API key from the environment. Only PRESENCE is read."""


def is_official(vendor: str, url: str) -> bool:
    """Whether `url` is `vendor`'s own API host; an empty url is the vendor default."""
    if not url:
        return True
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower() == OFFICIAL_HOSTS.get(vendor)
    except ValueError:
        return False


def claude_billing() -> str:
    """
    How THIS Claude Code is paid for — `subscription` (Pro/Max quota, the default and
    everything before 0.12.0) or `api` (an API key, billed per call).

    `api` only when it is unambiguous from what the hook can read cheaply: no
    subscription login in `~/.claude.json` (`oauthAccount`) AND a key source — one of
    `API_KEY_VARS` in the hook's environment, which is the session's, or an
    `apiKeyHelper` in the user settings. A machine signed in to a subscription that
    ALSO exports a key stays `subscription`: Claude Code uses such a key only once the
    user approved it, and that approval is recorded against the key's own characters,
    which this does not read. That case is reported as quota, exactly as before.
    """
    if claude_account_id():
        return "subscription"
    if any(os.environ.get(var, "").strip() for var in API_KEY_VARS):
        return "api"
    directory = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    settings = (Path(directory) if directory else Path.home() / ".claude") / "settings.json"
    try:
        helper = json.loads(settings.read_text(encoding="utf-8")).get("apiKeyHelper")
    except (OSError, ValueError, AttributeError):
        helper = None
    return "api" if helper else "subscription"


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
    Cumulative tokens per (MODEL, MINUTE) for one SESSION — its transcript plus every
    subagent transcript it spawned — and the number of records skipped because they did
    not come from an accepted entrypoint.

    THE MINUTE IS WHY THIS IS NOT KEYED BY MODEL ALONE. A session is a span: one left
    open all morning is a single session, and reported as a single row it can only be
    drawn spread evenly over every hour it was alive — a flat line where the truth was
    three bursts and a long idle. Every record carries the stamp of the response it
    holds, so the minute each one landed in is already known and only has to be kept.
    A minute and not an hour (0.8.x) because a minute is the finest bucket /usage
    draws: an hour row still had to be spread across its hour on the minute chart, and
    the flat stretch just moved inside the hour.

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

    THE WHOLE SET IS READ HERE; the hook reads only what is new — see `advance`, which
    this is built on, starting from an empty state.
    """
    state = new_state(entrypoints)
    advance(state, transcript)
    return bucketize(state["per_key"]), state["skipped"], state["cwd"]


def new_state(entrypoints: set) -> dict:
    """
    What `advance` carries from one read of a session to the next.

    `per_key` holds the session's FULL cumulative totals so far — never a delta — so
    what is sent from it is still a total and the server's upsert still converges.
    `seen` is the dedup set across every file of the session, `files` the byte offset
    reached in each, and the minute its last stamped record belonged to.
    """
    return {
        "entrypoints": sorted(entrypoints),
        "files": {},
        "seen": set(),
        "skipped": 0,
        "cwd": "",
        "per_key": {},
    }


def advance(state: dict, transcript: Path) -> bool:
    """
    Fold every COMPLETE line written since the last call into `state`. Returns False
    when a file was truncated or replaced — the offsets no longer mean anything and
    the caller must start over from an empty state.

    Only whole lines are consumed: a line still being written has no newline yet, and
    consuming it would skip it for good (the full re-read used to shrug it off as a
    parse error and pick it up next time — an offset cannot).

    The inherited minute (see `_consume`) is carried PER FILE, not across files: a
    record's minute then depends only on the lines above it in its own file, so it is
    the same whichever order the files are read in and however many runs the reading
    took. A subagent file's leading unstamped records fall into `bucketize`'s fold
    instead of borrowing the parent's last minute.
    """
    entrypoints = set(state["entrypoints"])
    # Parent first: it is the session's own record of where it ran, and a subagent
    # inherits that directory rather than defining it.
    for path in [transcript, *nested_transcripts(transcript)]:
        try:
            info = path.stat()
        except OSError:
            continue
        mark = state["files"].setdefault(str(path), {"offset": 0, "ino": info.st_ino, "minute": ""})
        if info.st_size < mark["offset"] or (mark.get("ino") and mark["ino"] != info.st_ino):
            return False
        if info.st_size == mark["offset"]:
            continue
        try:
            with path.open("rb") as fh:
                fh.seek(mark["offset"])
                data = fh.read()
        except OSError:
            continue
        end = data.rfind(b"\n")
        if end < 0:
            continue
        mark["offset"] += end + 1
        mark["ino"] = info.st_ino
        for raw in data[:end].split(b"\n"):
            _consume(state, mark, raw.decode("utf-8", errors="replace"), entrypoints)
    return True


def _consume(state: dict, mark: dict, line: str, entrypoints: set) -> None:
    """One transcript line into the running tally. See `tally` for the rules."""
    per_key = state["per_key"]
    seen = state["seen"]
    line = line.strip()
    if not line:
        return
    try:
        record = json.loads(line)
    except ValueError:
        return  # not JSON at all — a whole line, so not a partial write
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return
    if str(record.get("entrypoint") or "") not in entrypoints:
        state["skipped"] += 1
        return
    state["cwd"] = state["cwd"] or str(record.get("cwd") or "")

    # Read before the usage block, so a record that spends nothing still moves the
    # minute on for the ones that follow it. A record whose own stamp is unusable
    # inherits the last one: a transcript is only ever appended to, so its neighbour
    # is the closest thing to an answer there is.
    own_minute = minute_of(record.get("timestamp"))
    stamp = str(record.get("timestamp") or "") if own_minute else ""
    mark["minute"] = own_minute or mark["minute"]
    minute = mark["minute"]

    message = record.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return

    # One API response, many transcript lines — count it once.
    key = message.get("id") or record.get("requestId") or record.get("uuid")
    if not key or key in seen:
        return
    seen.add(key)

    model = str(message.get("model") or "").strip()
    # `<synthetic>` is Claude Code speaking for itself (an API error rendered as an
    # assistant turn); no model ran and no quota was spent.
    if not model or model.startswith("<"):
        return

    # The per-TTL split is what `cache_creation` carries, and it is read rather than
    # assumed: the two TTLs weigh differently (x2 against x1.25) and both occur --
    # across 120 transcripts here, 95.7% of write tokens are 1-hour and 9% of
    # sessions write some 5-minute ones. The flat `cache_creation_input_tokens` is the
    # older shape and carries no TTL at all; it falls to the 1-hour bucket, the larger
    # case, since guessing the cheaper one would under-count that term by 37.5%.
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
        return

    entry = per_key.setdefault((model, minute), dict.fromkeys(counts, 0) | {"calls": 0})
    for field, value in counts.items():
        entry[field] += value
    entry["calls"] += 1
    # The row's real span inside its minute — the server clamps it there and the chart
    # files it whole in one bucket. Compared as text: these are all the same ISO shape
    # in the same zone, so `min` and `max` on the strings are `min` and `max` on the
    # instants.
    if stamp:
        entry["startedAt"] = min(entry.get("startedAt") or stamp, stamp)
        entry["endedAt"] = max(entry.get("endedAt") or stamp, stamp)


def num(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
"""The stamp shape both harnesses write. Anything else is not read — see `minute_of`."""


def minute_of(value) -> str:
    """
    A record's `timestamp` → the minute it belongs to, `2026-09-03T07:05`, still in UTC.

    Sliced rather than parsed, because the first 16 characters of an ISO-8601 UTC stamp
    ARE the minute and both harnesses write exactly that shape
    (`2026-09-03T07:05:17.365Z`). Parsing would buy nothing and cost the one thing that
    matters here: a stamp in some other shape — an offset, a local time, a missing `T` —
    must not be read into a minute that the offset makes wrong, so it is refused and the
    record inherits its neighbour's minute instead.

    UTC on purpose. The minute is half of an upsert key, so it has to mean the same
    thing on a laptop that travels; the /usage chart cuts its own buckets in whatever
    zone the reader is in, from the stamps, and never from this.
    """
    text = str(value or "")
    return text[:16] if ISO_UTC_RE.match(text) else ""


def bucketize(per_key: dict) -> dict:
    """
    Give every tally a minute, or take the minute off all of them.

    A record with no usable stamp inherits the minute of the one before it, so the only
    tallies that can arrive here without one are the ones before the FIRST stamped
    record in the file. Their minute is not knowable, so they are folded into the
    earliest minute their model has — the session's earliest if that model has none.

    They cannot simply be left as they are: the server refuses a report that names the
    bucket for some entries and not others, because writing both a per-minute row and a
    whole-session row for one model counts that model twice, and neither row looks
    wrong on its own.

    A transcript with no usable stamp ANYWHERE keeps no minute at all and reports the
    pre-0.8.0 whole-session shape, which every server still handles.
    """
    minutes = sorted({minute for _, minute in per_key if minute})
    if not minutes:
        return per_key

    merged: dict[tuple, dict] = {}
    for (model, minute), totals in per_key.items():
        if not minute:
            own = sorted(m2 for m, m2 in per_key if m == model and m2)
            minute = own[0] if own else minutes[0]
        into = merged.setdefault((model, minute), {})
        for field, value in totals.items():
            if field in ("startedAt", "endedAt"):
                kept = into.get(field)
                into[field] = min(kept or value, value) if field == "startedAt" \
                    else max(kept or value, value)
            else:
                into[field] = into.get(field, 0) + value
    return merged


MAX_ENTRIES = 1024
"""The server's ceiling on one report (`MAX_REPORTED_ENTRIES`). The largest session on
the machine this was written on had 262 (model, minute) entries across 2,411 sessions;
17 busy hours on one model would reach it."""


def wire_models(per_key: dict, cap: bool = True) -> list:
    """
    The `models` array of a report — one entry per (model, minute).

    `bucket` is carried as a full ISO stamp rather than the 16-character key, so that
    it reads as a time to anything that parses it; the server floors it back to the
    minute before it builds the upsert key, so the two cannot disagree. A server older
    than 0.9.0 ignores `bucketSize` and floors to the hour instead, folding the minutes
    back into the 0.8.x rows — the same totals, just coarser.

    Past `MAX_ENTRIES` only the newest WHOLE hours are sent. The older minutes are final
    (a transcript is only appended to) and went up with earlier reports; a report over
    the cap would be refused whole and take the newest minutes down with it. Whole
    hours, because the server deletes a 0.8.x hour row once minutes for that hour
    arrive, and half an hour of minutes would leave the other half counted nowhere.
    """
    entries = []
    for (model, minute), totals in sorted(per_key.items()):
        entry = {"model": model}
        if minute:
            entry["bucket"] = f"{minute}:00Z"
        entry.update({k: v for k, v in totals.items() if k not in ("startedAt", "endedAt")})
        for field in ("startedAt", "endedAt"):
            if minute and totals.get(field):
                entry[field] = totals[field]
        entries.append(entry)
    if cap and len(entries) > MAX_ENTRIES:
        # ponytail: drops the oldest hours of a session no earlier report covered
        # (hooks dead for 17+ busy hours); chunked posts if that ever shows up in logs.
        cut = sorted(e.get("bucket", "")[:13] for e in entries)[-MAX_ENTRIES]
        log(f"{len(entries)} entries: sending the hours after {cut}")
        entries = [e for e in entries if e.get("bucket", "")[:13] > cut]
    return entries


def session_id_of(payload: dict, transcript: Path) -> str:
    """The hook names it; the transcript filename is the fallback (it IS the id)."""
    for key in ("session_id", "sessionId"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return transcript.stem


def body_of(config: dict, session: str, models: dict, cwd: str, context: dict | None = None,
            cap: bool = True) -> dict:
    """
    The wire shape of one report — built in ONE place so `--print` cannot drift from
    what is actually sent, which is the only way to check this without a server.

    `provider` names WHICH CLI spent the quota. A server older than 0.7.0 ignores the
    field and files the row as `claude-cli`, which is what every row from here has
    always been, so sending it costs an old deploy nothing.

    `context` is what the HOOK's environment said (`hook_context`), captured when the
    hook fired: the report is built later, in a background worker that may be draining
    another session's hook, whose environment is not this session's.
    """
    context = context or hook_context()
    backend = tuple(context.get("backend") or ("firstParty", ""))
    if context.get("billing") == "api":
        # Money, not quota: the VENDOR in `provider`, which a server older than 0.12.0
        # refuses rather than filing it as `claude-cli` at $0 — see `post_status`.
        return {
            "sessionId": session,
            "provider": "anthropic",
            "billing": "api",
            "accountId": "",
            "project": project_of(cwd),
            "device": device_of(config),
            "backend": backend[0],
            "baseUrl": backend[1],
            "bucketSize": "minute",
            "models": wire_models(models, cap),
        }
    return {
        "sessionId": session,
        "provider": "claude-cli",
        "accountId": context.get("account") or "",
        "project": project_of(cwd),
        "device": device_of(config),
        "backend": backend[0],
        "baseUrl": backend[1],
        "bucketSize": "minute",
        "models": wire_models(models, cap),
    }


def hook_context() -> dict:
    """The environment-derived fields of a report, read in the hook's own process."""
    return {"backend": list(backend_of()), "account": claude_account_id(), "billing": claude_billing()}


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
"""The only hosts a report may go to over plain http — this machine itself."""


def url_allowed(url: str) -> bool:
    """
    https anywhere; plain http only to this machine.

    The reporting key rides in every report's Authorization header, so over http
    anyone on the path reads it — once a turn, from every machine. `install.sh`
    refuses such a URL up front; this refuses it again for a config edited by hand.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and (parts.hostname or "") in LOCAL_HOSTS


def configured(config: dict) -> bool:
    """Whether this box may report at all. Says why not, in the debug log."""
    if not (config["enabled"] and config["url"] and config["token"]):
        log("skipped: not configured (need url + token, and enabled)")
        return False
    if not url_allowed(config["url"]):
        log(f"skipped: {safe_url(config['url'])} is plain http to a remote host — "
            "the key would travel in the clear; use https://")
        return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """
    Refuse every redirect: the 3xx surfaces as an HTTPError instead of being followed.

    urllib follows a redirected POST as a GET — the body dropped, so the report is
    silently not made — and re-sends the Authorization header to wherever the
    redirect points, which need not be the platform at all.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

DROP_STATUSES = frozenset({400, 413, 422})

OLD_SERVER_REFUSAL = re.compile(rb"(?<!api_)provider_not_supported:(?:anthropic|openai)")
"""What a server from before per-call billing answers a `billing: 'api'` report with."""
"""Refusals that resending the same body can never turn into a success — the body
itself is wrong. Anything else (a 5xx, 429, an expired token, a misconfigured URL
answering 3xx, no answer at all) is spooled and tried again later."""


def post_status(config: dict, body: dict, user_agent: str = USER_AGENT) -> str:
    """
    POST one report to the ledger: "ok", "retry" (spool it) or "drop" (never will).

    Shared with the Codex reporter: the endpoint, the timeout, the failure handling
    and the User-Agent are properties of the PLATFORM, not of the CLI being reported
    on, and a second copy of them is a second place for the WAF lesson below to be
    forgotten.
    """
    session = body.get("sessionId")
    if not url_allowed(config.get("url") or ""):
        log(f"reported {session}: refused, not https")
        return "retry"
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
        with _OPENER.open(request, timeout=TIMEOUT_S) as response:
            log(f"reported {session}: {response.status} {response.read(200)!r}")
            return "ok"
    except urllib.error.HTTPError as err:
        detail = err.read(200)
        log(f"reported {session}: HTTP {err.code} {detail!r}")
        if body.get("billing") == "api" and OLD_SERVER_REFUSAL.search(detail or b""):
            # A server older than 0.12.0 does not know per-call billing and refuses the
            # vendor in `provider` — the refusal that keeps it from filing the session
            # as quota. Not the body's fault, so it is kept and sent again once the
            # server is upgraded, instead of being dropped for good.
            log(f"reported {session}: the platform predates per-call billing — kept for later")
            return "retry"
        return "drop" if err.code in DROP_STATUSES else "retry"
    except (urllib.error.URLError, OSError, ValueError) as err:
        log(f"reported {session}: {err}")
    return "retry"


def post(config: dict, body: dict, user_agent: str = USER_AGENT) -> bool:
    """`post_status`, as the yes/no older callers expect."""
    return post_status(config, body, user_agent) == "ok"


# ------------------------------------------------------------------------------------
# Delivery: the chunks, the spool
# ------------------------------------------------------------------------------------

def chunks_of(body: dict) -> list:
    """
    One body per request, each within the server's `MAX_ENTRIES`.

    Split at HOUR boundaries, never inside one: the server deletes a 0.8.x hour row
    as soon as minutes for that hour arrive, and half an hour of minutes would leave
    the other half counted nowhere. A single hour holds at most 60 minutes per model,
    so a group never outgrows a chunk on its own for any realistic model count.
    """
    models = body.get("models") or []
    if len(models) <= MAX_ENTRIES:
        return [body]
    hours: dict = {}
    for entry in models:
        hours.setdefault(str(entry.get("bucket", ""))[:13], []).append(entry)
    out, current = [], []
    for hour in sorted(hours):
        group = hours[hour]
        if current and len(current) + len(group) > MAX_ENTRIES:
            out.append(current)
            current = []
        current = current + group
    if current:
        out.append(current)
    return [dict(body, models=chunk) for chunk in out]


def post_chunks(config: dict, body: dict, user_agent: str = USER_AGENT) -> str:
    """Post every chunk; the worst answer wins (retry > drop > ok)."""
    outcome = "ok"
    for chunk in chunks_of(body):
        result = post_status(config, chunk, user_agent)
        if result == "retry":
            return "retry"
        if result == "drop":
            outcome = "drop"
    return outcome


SPOOL_MAX_AGE_S = 14 * 86400
"""A spooled report older than this is dropped unsent. The transcript is still on disk;
deleting the state dir re-sends everything it holds."""

SPOOL_FLUSH_MAX = 20
"""Spooled reports tried per run — a platform back from an outage is not hammered."""


def state_root() -> Path:
    """Where the reporters keep their offsets, queue and spool. Regenerable: losing it
    costs one full re-read and re-send, which the server's upsert absorbs."""
    named = os.environ.get("AGENTS_USAGE_STATE_DIR", "").strip()
    if named:
        return Path(named)
    cache = os.environ.get("XDG_CACHE_HOME", "").strip()
    return (Path(cache) if cache else Path.home() / ".cache") / "agents-usage"


def _key_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def write_json(path: Path, value) -> bool:
    """Temp-and-rename, 0600: a reader never sees half a file, and a spooled body
    names the account, the project and the device."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600) if hasattr(os, "fchmod") else None
            json.dump(value, fh)
        os.replace(temporary, path)
        return True
    except OSError as err:
        log(f"write {path.name} failed: {err!r}")
        return False


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def merge_bodies(older: dict, newer: dict) -> dict:
    """
    One report carrying both: every (model, bucket) entry, the NEWER total winning.

    Every entry is a cumulative total, so for one key the newer reading is simply the
    right one. Merging BEFORE sending is what keeps an older spooled total from being
    delivered after a newer one and overwriting it with a smaller number.
    """
    entries = {}
    for body in (older, newer):
        for entry in body.get("models") or []:
            entries[(entry.get("model"), entry.get("bucket", ""))] = entry
    merged = dict(newer)
    merged["models"] = [entries[k] for k in sorted(entries, key=lambda k: (str(k[1]), str(k[0])))]
    return merged


def deliver(config: dict, body: dict, spool: Path, user_agent: str = USER_AGENT,
            spool_key: str | None = None) -> str:
    """
    Send one report, folding in whatever is spooled for the same session first, and
    spool the lot when the platform cannot take it now. Returns the outcome.

    `spool_key` defaults to the session id. A reporter that sends one session as more
    than one body — OpenCode, one per provider — names a key per body, or the merge
    would file one provider's entries under the other's.
    """
    parked = spool / f"{_key_of(spool_key or str(body.get('sessionId')))}.json"
    earlier = read_json(parked)
    if isinstance(earlier, dict):
        body = merge_bodies(earlier, body)
    outcome = post_chunks(config, body, user_agent)
    if outcome == "retry":
        write_json(parked, body)
    else:
        try:
            parked.unlink()
        except OSError:
            pass
    return outcome


def flush_spool(config: dict, spool: Path, user_agent: str = USER_AGENT) -> None:
    """Resend what earlier runs could not deliver, oldest first; stop at the first
    one the platform still cannot take — it is down, and the rest would fail too."""
    try:
        parked = sorted(spool.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    now = __import__("time").time()
    tried = 0
    for path in parked:
        try:
            if now - path.stat().st_mtime > SPOOL_MAX_AGE_S:
                log(f"spool: {path.name} expired unsent")
                path.unlink()
                continue
        except OSError:
            continue
        if tried >= SPOOL_FLUSH_MAX:
            break
        body = read_json(path)
        if not isinstance(body, dict):
            path.unlink(missing_ok=True)
            continue
        tried += 1
        outcome = post_chunks(config, body, user_agent)
        if outcome == "retry":
            break
        path.unlink(missing_ok=True)


# ------------------------------------------------------------------------------------
# Incremental reporting: the offsets, and only what changed
# ------------------------------------------------------------------------------------

STATE_VERSION = 1


def _dump_state(state: dict) -> dict:
    return {
        "version": STATE_VERSION,
        "entrypoints": state["entrypoints"],
        "files": state["files"],
        "seen": sorted(state["seen"]),
        "skipped": state["skipped"],
        "cwd": state["cwd"],
        "per_key": [[m, minute, totals] for (m, minute), totals in state["per_key"].items()],
        "sent": state.get("sent", {}),
    }


def _load_state(raw, entrypoints: set):
    """A saved state, or None when there is none or it was taken under other rules."""
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return None
    if raw.get("entrypoints") != sorted(entrypoints):
        return None  # the filter changed: the kept totals were counted by the old one
    try:
        return {
            "entrypoints": raw["entrypoints"],
            "files": dict(raw["files"]),
            "seen": set(raw["seen"]),
            "skipped": int(raw["skipped"]),
            "cwd": str(raw["cwd"]),
            "per_key": {(m, minute): dict(totals) for m, minute, totals in raw["per_key"]},
            "sent": dict(raw.get("sent") or {}),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _sent_key(key: tuple) -> str:
    return f"{key[0]}\x1f{key[1]}"


def report_transcript(config: dict, session: str, transcript: Path, context: dict | None = None,
                      root: Path | None = None) -> str:
    """
    Report one session INCREMENTALLY: read only the bytes written since the last run,
    and send only the (model, minute) totals that changed. Returns the outcome, or
    "idle" when nothing changed.

    The cumulative contract is untouched. What is kept between runs is the FULL
    running total per (model, minute), and a changed minute is sent as its whole
    total, never as a delta — so the server's upsert converges exactly as it did when
    every run re-read everything, a repeated send is still a no-op, and a lost state
    file costs one full re-read and re-send and nothing else.

    Only the reading moved. Re-parsing a long session and its subagents on every turn
    took seconds (2.2s measured on one session), inside a hook the session waits on.
    """
    root = root or state_root() / "claude"
    entrypoints = set(config["entrypoints"])
    state_path = root / "state" / f"{_key_of(str(transcript.resolve()))}.json"
    state = _load_state(read_json(state_path), entrypoints) or dict(new_state(entrypoints), sent={})
    if not advance(state, transcript):
        # Truncated or replaced: start over, and re-send all of it.
        log(f"{transcript.name}: rewritten since the last read — re-reading it whole")
        state = dict(new_state(entrypoints), sent={})
        advance(state, transcript)
    if state["skipped"]:
        log(f"{transcript.name}: skipped {state['skipped']} record(s) from another entrypoint")

    models = bucketize(state["per_key"])
    changed = {k: v for k, v in models.items() if state["sent"].get(_sent_key(k)) != v}
    outcome = "idle"
    if changed:
        body = body_of(config, session, changed, state["cwd"], context, cap=False)
        outcome = deliver(config, body, root / "spool")
        # Delivered, spooled or refused for good — either way this run is done with
        # these totals: the spool now carries a retry, not the state.
        for key, totals in changed.items():
            state["sent"][_sent_key(key)] = totals
    write_json(state_path, _dump_state(state))
    return outcome


# ------------------------------------------------------------------------------------
# The hook: enqueue, detach, and one worker at a time
# ------------------------------------------------------------------------------------

def enqueue(root: Path, payload: dict, context: dict) -> bool:
    """Park the hook's payload for the worker. Named by time so the queue drains in
    the order the hooks fired."""
    name = f"{__import__('time').time_ns():020d}-{os.getpid()}.json"
    return write_json(root / "queue" / name, {"payload": payload, "context": context})


def spawn_worker(script: Path) -> None:
    """
    Start `script --worker` detached, so the hook returns at once.

    Its own session (process group) so the harness reaping the hook does not take the
    worker with it; every stdio stream on /dev/null so nothing holds the hook's pipes
    open behind it.
    """
    sp = subprocess
    kwargs = {"stdin": sp.DEVNULL, "stdout": sp.DEVNULL, "stderr": sp.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(sp, "DETACHED_PROCESS", 0) | getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        sp.Popen([sys.executable, str(script), "--worker"], **kwargs)
    except OSError as err:
        log(f"could not start the worker: {err!r}")


class WorkerLock:
    """
    `flock -n` on `<root>/worker.lock`: one worker per reporter at a time, and a
    second one leaves at once instead of queueing behind the first.

    No lost reports: whoever holds the lock drains the queue until it is empty, and
    re-checks after letting go (see `run_worker`). Where `fcntl` does not exist
    (Windows) there is no lock — two workers then only ever re-send the same
    cumulative totals, which the upsert absorbs.
    """

    def __init__(self, root: Path, blocking: bool = False) -> None:
        self.path = root / "worker.lock"
        self.blocking = blocking
        self.handle = None

    def acquire(self) -> bool:
        try:
            import fcntl  # noqa: PLC0415
        except ImportError:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.path, "a+")  # noqa: SIM115
            flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
            fcntl.flock(self.handle.fileno(), flags)
            return True
        except OSError:
            if self.handle:
                self.handle.close()
                self.handle = None
            return False

    def release(self) -> None:
        if self.handle:
            self.handle.close()  # closing the descriptor drops the flock
            self.handle = None


def drain(root: Path, handle) -> int:
    """Process every queued payload; returns how many. The newest payload per
    transcript is all that matters — the report is cumulative — so repeats collapse.
    A payload that names no transcript (OpenCode's: one database, many sessions)
    collapses on its session id instead, never into one shared empty key."""
    try:
        items = sorted((root / "queue").glob("*.json"))
    except OSError:
        return 0
    latest: dict = {}
    for item in items:
        queued = read_json(item)
        try:
            item.unlink()
        except OSError:
            pass
        if not isinstance(queued, dict) or not isinstance(queued.get("payload"), dict):
            continue
        payload = queued["payload"]
        where = str(payload.get("transcript_path") or payload.get("transcriptPath")
                    or payload.get("session_id") or payload.get("sessionId") or "")
        latest.pop(where, None)  # re-insert so the order stays "most recent last"
        latest[where] = queued
    for queued in latest.values():
        try:
            handle(queued["payload"], queued.get("context") or {})
        except Exception as err:  # noqa: BLE001 - one bad session must not stop the rest
            log(f"worker: {err!r}")
    return len(items)


def run_worker(root: Path, config: dict, handle, user_agent: str, rounds: int = 5) -> int:
    """
    Drain the queue behind the lock, then flush the spool.

    `flock -n` alone loses the payload that lands just as the holder finishes: its own
    worker found the lock taken and left, and the holder had already looked. So the
    holder looks again after letting go, and goes round once more if anything arrived.
    """
    for _ in range(rounds):
        lock = WorkerLock(root)
        if not lock.acquire():
            return 0
        try:
            drain(root, handle)
            flush_spool(config, root / "spool", user_agent)
        finally:
            lock.release()
        try:
            if not any((root / "queue").glob("*.json")):
                break
        except OSError:
            break
    return 0


def read_payload() -> dict:
    payload = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def hook_entry(root: Path, script: Path, payload: dict, context: dict, config: dict, handle,
               user_agent: str) -> int:
    """
    The hook's whole job: queue the payload and hand it to a background worker.

    It returns in milliseconds whatever the platform is doing — reading a long
    transcript, a slow network, a platform that is down — where it used to hold the
    session up to the hook's 10s timeout. `AGENTS_USAGE_SYNC=1` runs the worker in the
    foreground instead (debugging, tests).
    """
    if not enqueue(root, payload, context):
        return 0
    if os.environ.get("AGENTS_USAGE_SYNC") == "1":
        return run_worker(root, config, handle, user_agent)
    spawn_worker(script)
    return 0


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


def handle_payload(config: dict):
    """What the worker does with one queued hook payload."""
    def handle(payload: dict, context: dict) -> None:
        for transcript in transcripts_of(payload, []):
            if transcript.is_file():
                report_transcript(config, session_id_of(payload, transcript), transcript, context)
    return handle


def main() -> int:
    argv = sys.argv[1:]
    if "--set-device" in argv:
        return device_command(argv)
    dry_run = "--print" in argv
    root = state_root() / "claude"

    # A backfill takes no hook payload, and a caller that leaves stdin open without
    # ever writing to it (a tool runner, a pipe) would otherwise hang the sweep.
    payload = {} if "--worker" in argv or "--backfill" in argv else read_payload()
    config = load_config()

    if dry_run:
        # A full, stateless read — what the session totals to right now.
        for transcript in transcripts_of(payload, argv):
            if not transcript.is_file():
                continue
            models, _skipped, cwd = tally(transcript, config["entrypoints"])
            if models:
                print(json.dumps(body_of(config, session_id_of(payload, transcript), models, cwd), indent=2))
        return 0

    if not configured(config):
        return 0

    if "--worker" in argv:
        return run_worker(root, config, handle_payload(config), USER_AGENT)

    if "--backfill" in argv:
        # A human at a terminal: synchronous, and it WAITS for a running worker rather
        # than skipping, so the two never write one session's state at once.
        lock = WorkerLock(root, blocking=True)
        lock.acquire()
        try:
            context = hook_context()
            for transcript in transcripts_of(payload, argv):
                if transcript.is_file():
                    report_transcript(config, session_id_of(payload, transcript), transcript, context)
            flush_spool(config, root / "spool")
        finally:
            lock.release()
        return 0

    return hook_entry(root, Path(__file__).resolve(), payload, hook_context(), config,
                      handle_payload(config), USER_AGENT)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # never take a session down with us
        log(f"crashed: {err!r}")
        sys.exit(0)
