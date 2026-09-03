#!/usr/bin/env sh
# ============================================================
# agents-usage installer — pick a CLI, answer three questions, done.
#
# What it does, in order:
#   1. works out which CLIs are on this machine and asks which to report from;
#   2. asks for the platform URL, the API token and an optional device label,
#      offering whatever a previous install already wrote as the default;
#   3. writes ONE config file, chmod 600 (both reporters read either location);
#   4. registers the marketplace WITH AUTO-UPDATE and installs the plugin.
#
# It is safe to re-run: every step is a no-op when it is already done, and an
# existing config is edited rather than replaced — so answering "keep" to the
# token question leaves the one already on disk untouched.
#
# Non-interactive (CI, a fleet script), from a clone or straight off the web:
#   install.sh --platform both --url https://… --token aur_… --device dev-pc --yes
#   curl -fsSL https://raw.githubusercontent.com/vannguyen799/agents-usage/refs/heads/main/scripts/install.sh \
#     | sh -s -- --platform both --url https://… --token aur_… --yes
#
# Piped there is no terminal to ask into, so every answer must arrive as a flag —
# `--url` and `--token` above all, since an installer that reached the end with no
# key would leave a plugin that reports nothing and never says why.
# ============================================================
set -eu

MARKETPLACE="vt-plugins"
PLUGIN="agents-usage"
GITHUB_REPO="vannguyen799/agents-usage"
DEFAULT_URL="https://agents.zynalgo.com"

PLATFORM=""; URL=""; TOKEN=""; DEVICE=""; SOURCE="github"; ASSUME_YES=0
KEEP="__keep__"   # sentinel: the answer "leave what is already there alone"

usage() {
  cat <<'TXT'
Usage: install.sh [options]

  --platform claude|codex|both   which CLI to report from (default: ask)
  --url URL                      the agent platform's base URL
  --token aur_…                  a reporting key (see below)
  --device LABEL                 what to call this machine on /usage (default user@host)
  --source github|local          where the plugin is installed from
                                 (default github — it is the one that auto-updates)
  --yes                          take the defaults, ask nothing
  -h, --help                     this

Mint the key in the platform UI, on /usage → "Report a machine". What it mints is a
REPORTING key (`aur_…`) whose entire vocabulary is `usage:write` — it belongs to no
space and can read nothing back, which is what a file on a laptop should be worth.
A space-scoped `apk_…` token minted before that panel existed still reports.
TXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --platform) PLATFORM="${2:-}"; shift 2 ;;
    --url)      URL="${2:-}"; shift 2 ;;
    --token)    TOKEN="${2:-}"; shift 2 ;;
    --device)   DEVICE="${2:-}"; shift 2 ;;
    --source)   SOURCE="${2:-}"; shift 2 ;;
    --yes|-y)   ASSUME_YES=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
warn() { printf '!  %s\n' "$*" >&2; }
die()  { printf 'x  %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

# Run a command, indent what it printed, and return ITS status — which piping
# straight into `sed` would throw away, leaving every `|| fallback` below dead code.
run_step() {
  _status=0
  _out=$("$@" 2>&1) || _status=$?
  [ -n "$_out" ] && printf '%s\n' "$_out" | grep -v '^WARNING: proceeding' | sed 's/^/   /' || true
  return "$_status"
}

# A prompt with a default. Returns the default unchanged under --yes, so the same
# script serves an interactive install and an unattended one.
ask() {
  _prompt="$1"; _default="$2"
  if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then printf '%s' "$_default"; return; fi
  if [ -n "$_default" ]; then printf '%s [%s]: ' "$_prompt" "$_default" >&2
  else printf '%s: ' "$_prompt" >&2; fi
  IFS= read -r _answer || _answer=""
  printf '%s' "${_answer:-$_default}"
}

# The same, with the terminal echo off — a token must not be left on screen or in
# a scrollback buffer. `stty` rather than `read -s`, which is a bashism.
ask_secret() {
  _prompt="$1"; _default="$2"
  if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then printf '%s' "$_default"; return; fi
  printf '%s' "$_prompt" >&2
  [ "$_default" = "$KEEP" ] && printf ' [keep the one already saved]' >&2
  printf ': ' >&2
  _saved=$(stty -g 2>/dev/null || true)
  [ -n "$_saved" ] && stty -echo 2>/dev/null || true
  IFS= read -r _answer || _answer=""
  [ -n "$_saved" ] && stty "$_saved" 2>/dev/null || true
  printf '\n' >&2
  printf '%s' "${_answer:-$_default}"
}

PY=""
for candidate in python3 python; do have "$candidate" && { PY="$candidate"; break; }; done
[ -n "$PY" ] || die "python3 is required — it is what the reporter itself runs on."

# Where this script is running FROM — or nothing at all, which is the normal case
# when it arrives down a pipe. `curl … | sh` leaves "$0" as the shell's own name, so
# `dirname "$0"` is the CALLER's directory: `--source local` would hand the CLI some
# unrelated tree to install, and the hints at the end would name a path that holds no
# reporter. Resolve it once, prove it with a sibling only this repo has, and treat
# "there is no directory" as a state to branch on rather than a path to guess at.
SCRIPT_DIR=""
if [ -n "${0:-}" ] && [ -f "$0" ]; then
  _dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd) || _dir=""
  [ -n "$_dir" ] && [ -f "$_dir/report_usage.py" ] && SCRIPT_DIR="$_dir"
fi

# The reporter this machine will actually run. Piped, the only copy is the one the
# install just cloned, so look there too — each CLI keeps it in its own place.
reporter_dir() {
  for _candidate in \
    "$SCRIPT_DIR" \
    "$HOME/.claude/plugins/marketplaces/$MARKETPLACE/scripts" \
    "$HOME/.codex/plugins/cache/$MARKETPLACE/$PLUGIN/scripts"
  do
    [ -n "$_candidate" ] && [ -f "$_candidate/report_usage.py" ] \
      && { printf '%s' "$_candidate"; return 0; }
  done
  return 1
}

# ------------------------------------------------------------
# 1. which CLI
# ------------------------------------------------------------
have claude && HAS_CLAUDE=1 || HAS_CLAUDE=0
have codex  && HAS_CODEX=1  || HAS_CODEX=0
[ "$HAS_CLAUDE" = 1 ] || [ "$HAS_CODEX" = 1 ] \
  || die "neither \`claude\` nor \`codex\` is on PATH — nothing to install into."

if [ -z "$PLATFORM" ]; then
  step "Which CLI should report its usage?"
  say "  1) Claude Code$([ "$HAS_CLAUDE" = 1 ] && echo '' || echo '   (not installed)')"
  say "  2) Codex CLI$([ "$HAS_CODEX" = 1 ] && echo '' || echo '     (not installed)')"
  say "  3) both"
  if   [ "$HAS_CLAUDE" = 1 ] && [ "$HAS_CODEX" = 1 ]; then _default=3
  elif [ "$HAS_CLAUDE" = 1 ]; then _default=1
  else _default=2; fi
  case "$(ask "  choice" "$_default")" in
    1) PLATFORM=claude ;;
    2) PLATFORM=codex ;;
    3) PLATFORM=both ;;
    *) die "pick 1, 2 or 3." ;;
  esac
fi

case "$PLATFORM" in
  claude) WANT_CLAUDE=1; WANT_CODEX=0 ;;
  codex)  WANT_CLAUDE=0; WANT_CODEX=1 ;;
  both)   WANT_CLAUDE=1; WANT_CODEX=1 ;;
  *) die "--platform must be claude, codex or both." ;;
esac
[ "$WANT_CLAUDE" = 1 ] && [ "$HAS_CLAUDE" = 0 ] && die "\`claude\` is not on PATH."
[ "$WANT_CODEX"  = 1 ] && [ "$HAS_CODEX"  = 0 ] && die "\`codex\` is not on PATH."

# The config goes where the CLI being installed for keeps its own files. Both
# reporters read both locations, so one file is enough even for `both`.
if [ "$WANT_CLAUDE" = 1 ]; then CONFIG="$HOME/.claude/agents-usage.json"
else CONFIG="$HOME/.codex/agents-usage.json"; fi

# ------------------------------------------------------------
# 2. the answers
# ------------------------------------------------------------
# Defaults come from whatever is already installed, so re-running this is a way to
# change ONE field without having to retype a token.
EXISTING=$("$PY" - "$HOME" <<'PY'
import json, sys
from pathlib import Path
home = Path(sys.argv[1])
for path in (home / ".claude/agents-usage.json", home / ".codex/agents-usage.json"):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if isinstance(config, dict):
        print(json.dumps({
            "url": config.get("url", ""),
            "device": config.get("device", ""),
            "hasToken": bool(config.get("token")),
            "path": str(path),
        }))
        break
PY
) || EXISTING=""

field() { printf '%s' "$EXISTING" | "$PY" -c "import json,sys;d=sys.stdin.read().strip();print((json.loads(d) if d else {}).get('$1',''))"; }

if [ -n "$EXISTING" ]; then say "found an existing config at $(field path)"; fi

step "Where does this machine report to?"
[ -n "$URL" ] || URL=$(ask "  platform URL" "$(field url || true)")
[ -n "$URL" ] || URL="$DEFAULT_URL"
case "$URL" in http://*|https://*) ;; *) die "the URL must start with http:// or https://" ;; esac
URL=$(printf '%s' "$URL" | sed 's#/*$##')

if [ -z "$TOKEN" ]; then
  if [ "$(field hasToken)" = "True" ]; then TOKEN=$(ask_secret "  API token" "$KEEP")
  else TOKEN=$(ask_secret "  reporting key (aur_…)" ""); fi
fi
if [ "$TOKEN" != "$KEEP" ]; then
  [ -n "$TOKEN" ] || die "no key — nothing would ever be reported. Mint one on /usage → Report a machine."
  # Both prefixes are legitimate: `aur_` is a reporting key, `apk_` a space-scoped API
  # token from before reporting keys existed, and those still report.
  case "$TOKEN" in aur_*|apk_*) ;; *) warn "that looks like neither an aur_ key nor an apk_ token; using it anyway." ;; esac
fi

[ -n "$DEVICE" ] || DEVICE=$(ask "  device label (blank = user@host)" "$(field device || true)")

# ------------------------------------------------------------
# 3. the config file
# ------------------------------------------------------------
step "Writing $CONFIG"
"$PY" - "$CONFIG" "$URL" "$TOKEN" "$DEVICE" "$KEEP" <<'PY'
import json, os, sys, tempfile
from pathlib import Path

path, url, token, device, keep = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
path.parent.mkdir(parents=True, exist_ok=True)

# Read-modify-write: a config may carry `entrypoints`, `originators` or anything a
# later version adds, and an installer that replaced the file would silently drop it.
try:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        config = {}
except (OSError, ValueError):
    config = {}

config["url"] = url
if token != keep:
    config["token"] = token
config["enabled"] = True
if device:
    config["device"] = device
else:
    config.pop("device", None)

# Written 0600 from the start and renamed into place: a token must never exist,
# even for an instant, in a file the rest of the machine can read.
handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".agents-usage-")
with os.fdopen(handle, "w", encoding="utf-8") as fh:
    os.fchmod(fh.fileno(), 0o600)
    json.dump(config, fh, indent=2)
    fh.write("\n")
os.replace(temporary, path)
print(f"   url={url}  token={'kept' if token == keep else 'set'}")
PY

# ------------------------------------------------------------
# 4. install, with updates left switched on
# ------------------------------------------------------------
case "$SOURCE" in
  github) CLAUDE_SOURCE="$GITHUB_REPO"; CODEX_SOURCE="$GITHUB_REPO" ;;
  local)
    [ -n "$SCRIPT_DIR" ] || die "--source local needs this repo on disk, and this script is being piped — clone it and run scripts/install.sh, or drop the flag."
    REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
    CLAUDE_SOURCE="$REPO_ROOT"; CODEX_SOURCE="$REPO_ROOT" ;;
  *) die "--source must be github or local." ;;
esac

if [ "$WANT_CLAUDE" = 1 ]; then
  step "Claude Code"
  # Adding a marketplace that is already there fails; updating it is then the
  # right thing to do anyway, so the failure is the signal rather than an error.
  if ! run_step claude plugin marketplace add "$CLAUDE_SOURCE"; then
    run_step claude plugin marketplace update "$MARKETPLACE" || true
  fi

  # THE auto-update switch. The CLI has no flag for it: `autoUpdate` on the
  # marketplace entry in settings.json is what makes Claude Code refresh the
  # marketplace on its own, and without it every machine keeps whatever version it
  # first installed — which is how one workstation here ran a broken reporter for a
  # week while every report bounced off a WAF.
  "$PY" - "$HOME/.claude/settings.json" "$MARKETPLACE" <<'PY'
import json, os, sys, tempfile
from pathlib import Path

path, marketplace = Path(sys.argv[1]), sys.argv[2]
try:
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError
except (OSError, ValueError):
    print("   ! could not read settings.json — turn auto-update on from /plugin instead")
    sys.exit(0)

entry = (settings.get("extraKnownMarketplaces") or {}).get(marketplace)
if not isinstance(entry, dict):
    print(f"   ! {marketplace} is not in extraKnownMarketplaces — auto-update not set")
    sys.exit(0)
if entry.get("autoUpdate") is True:
    print("   auto-update already on")
    sys.exit(0)

entry["autoUpdate"] = True
handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-")
with os.fdopen(handle, "w", encoding="utf-8") as fh:
    os.fchmod(fh.fileno(), path.stat().st_mode & 0o777)
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(temporary, path)
print("   auto-update on")
PY

  run_step claude plugin install "$PLUGIN@$MARKETPLACE" --scope user --yes || \
    warn "install reported a problem — \`claude plugin list\` will say whether it landed."
fi

if [ "$WANT_CODEX" = 1 ]; then
  step "Codex CLI"
  run_step codex plugin marketplace add "$CODEX_SOURCE" || true
  run_step codex plugin add "$PLUGIN@$MARKETPLACE" || \
    warn "install reported a problem — \`codex plugin list\` will say whether it landed."
  if [ "$SOURCE" = github ]; then
    # Codex refreshes configured GIT marketplaces by itself at startup; this is only
    # to prove the path works now rather than at the next release.
    run_step codex plugin marketplace upgrade || true
  else
    warn "a LOCAL codex marketplace is COPIED into ~/.codex/plugins/cache at install."
    warn "It will not follow a \`git pull\` — re-run this with --source github for updates."
  fi
fi

# ------------------------------------------------------------
step "Done"

# What this machine will be CALLED on /usage — read back from the reporter that was
# just installed rather than guessed at here, so the label printed is the label sent.
REPORTER=$(reporter_dir) || REPORTER=""
[ -n "$REPORTER" ] && AGENTS_USAGE_CONFIG="$CONFIG" \
  "$PY" "$REPORTER/report_usage.py" --set-device 2>/dev/null | sed 's/^/   /'

say "Restart the CLI you installed into — hooks are read at startup."
[ "$WANT_CODEX" = 1 ] && say "Codex asks once to TRUST this plugin's hooks; until you do, nothing is reported."
say ""
say "Check it: AGENTS_USAGE_DEBUG=1 in the environment writes every attempt to"
say "  $(dirname "$CONFIG")/agents-usage.log"
if [ -n "$REPORTER" ]; then
  say "Catch up sessions that ended without a hook (safe to re-run, the report is cumulative):"
  [ "$WANT_CLAUDE" = 1 ] && say "  $REPORTER/report-usage.sh --backfill 7"
  [ "$WANT_CODEX"  = 1 ] && say "  $REPORTER/report-codex-usage.sh --backfill 7"
fi
exit 0
