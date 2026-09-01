#!/usr/bin/env python3
"""
Pin how this machine NAMES itself. No network, no CLI install, no config of yours
touched — everything runs against a throwaway config in a temp dir.

    python3 scripts/smoke_device.py

The device label is the one field a machine sends about ITSELF, and every way of
getting it wrong is quiet: the report still posts and the row still appears, under a
name that splits one laptop in two on the /usage breakdown or does not identify it at
all. The write path is worse than quiet — the config it edits also holds the URL and
the API token, so a careless read-modify-write stops every report from the box and
says nothing.
"""
import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_usage as claude  # noqa: E402
import report_codex_usage as codex  # noqa: E402

FAILURES = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL {name}\n       got  {got}\n       want {want}")


def naming(user="vt", host="dev-pc") -> None:
    """Speak for `whoami` and `hostname`, which a test box answers for differently."""
    claude.getpass.getuser = lambda: user
    claude.socket.gethostname = lambda: host


def config(**contents) -> Path:
    """A throwaway config, pointed at by AGENTS_USAGE_CONFIG like a real override."""
    path = Path(tempfile.mkdtemp(prefix="agents-usage-smoke-")) / "agents-usage.json"
    path.write_text(json.dumps(contents), encoding="utf-8")
    os.environ["AGENTS_USAGE_CONFIG"] = str(path)
    return path


def label() -> str:
    return claude.device_of(claude.load_config())


def rename(name: str) -> None:
    """`--set-device` reports to the human who typed it; here that is just noise."""
    with contextlib.redirect_stdout(io.StringIO()):
        claude.set_device(name)


os.environ.pop("AGENTS_USAGE_DEVICE", None)

print("the default is user@host")
naming()
config(url="http://x", token="apk_t")
check("what whoami and hostname say", label(), "vt@dev-pc")
naming(host="dev-pc.corp.example.com")
check("the domain is dropped — it names a network, not a box", label(), "vt@dev-pc")
naming(user="", host="dev-pc")
check("half a label still names something", label(), "dev-pc")

print("\na name that was chosen wins")
naming()
config(url="http://x", token="apk_t", device="build-box")
check("the config file beats the derived default", label(), "build-box")
os.environ["AGENTS_USAGE_DEVICE"] = "one-shell-only"
check("and the environment beats the file", label(), "one-shell-only")
os.environ.pop("AGENTS_USAGE_DEVICE")

print("\nboth reporters name one machine once")
# Not "the same string" — the SAME function. Two implementations agreeing today is
# what the machineID/installation_id split looked like right up until it did not.
check("Codex resolves the label through the Claude reporter's own function",
      codex.device_of is claude.device_of, True)
check("…so one box reads as one row on both", codex.device_of(claude.load_config()), "build-box")

print("\nthe id is ours, not a harness's")
naming(user="", host="")
claude.machine_seed = lambda: ""            # a box whose OS will not name itself
path = config(url="http://x", token="apk_t")
first = label()
check("generated when the box will name neither user nor host", len(first), 12)
check("stable — the second call reads what the first stored", label(), first)
check("kept where BOTH reporters look", json.loads(path.read_text())["deviceId"], first)
check("and it is not Claude Code's machineID", first == claude.machine_id(), False)

print("\nthe id follows the DEVICE, so a reinstall is the same machine")
SEED = "6b1e0f0d4c9a4c2f8e7d5b3a1c9f0e2d"   # what the OS calls this box
claude.machine_seed = lambda: SEED
config(url="http://x", token="apk_t")       # a config with no deviceId in it: a fresh install
derived = label()
config(url="http://x", token="apk_t")       # …and again, the config gone entirely
check("re-derived rather than re-invented", label(), derived)
check("the raw OS id is never what gets sent", SEED.startswith(derived), False)
check("…it is hashed with an app salt",
      derived, hashlib.sha256(f"agents-usage:{SEED}".encode("utf-8")).hexdigest()[:12])
config(url="http://x", token="apk_t", deviceId="from-when-this-box-first-reported")
check("but an id already reported under wins over deriving a new one",
      label(), "from-when-this-box-first-reported")

print("\nthe write cannot cost this box its credentials")
path = config(url="http://x", token="apk_t", entrypoints=["cli", "sdk-cli"])
rename("dev-pc")
saved = json.loads(path.read_text())
check("the token survives being renamed", saved.get("token"), "apk_t")
check("so does everything a later version added", saved.get("entrypoints"), ["cli", "sdk-cli"])
check("the name landed", saved.get("device"), "dev-pc")
check("0600 — the file holds an API token",
      stat.S_IMODE(path.stat().st_mode), 0o600)
naming()
rename("")
check("an empty name gives back the derived one", label(), "vt@dev-pc")
check("…by removing the key, not by storing a blank",
      "device" in json.loads(path.read_text()), False)

print("\na RANDOM id that cannot be stored is never sent")
# A fresh id per session would file every session as its own device — worse than the
# harness id it replaced, so an unwritable box falls back instead of inventing one.
# A DERIVED id needs no file to stay stable, which is why it is preferred.
naming(user="", host="")
claude.machine_seed = lambda: ""
missing = Path(tempfile.mkdtemp(prefix="agents-usage-smoke-")) / "gone" / "agents-usage.json"
os.environ["AGENTS_USAGE_CONFIG"] = str(missing)
claude.update_config = lambda changes, preferred=None: None
claude.machine_id = lambda: "cc-machine-id"
check("falls back rather than sending a value nothing will remember",
      label(), "cc-machine-id")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all good")
