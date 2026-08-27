# agents-usage

Reports **this machine's Claude Code token usage** into the agent platform's usage
ledger, so `/usage` shows a subscription's whole quota burn — not just the half the
platform spent itself.

The platform meters agents that run on the `claude-cli` provider. A human typing into
Claude Code on the same login spends the same Pro/Max quota and the platform sees none
of it. This plugin closes that gap.

## How it works

Two hooks (`Stop`, `SessionEnd`) run one script. It re-reads the session transcript,
sums tokens per model, and POSTs the **cumulative** total to
`POST /api/v1/usage/sessions`. The server upserts on `cli:<sessionId>:<model>`.

**Subagents count too.** Claude Code writes what the Task tool spawns beside the session
rather than into it — `<project>/<session-id>/subagents/agent-*.jsonl` — and none of it
reaches the parent transcript, so reading the session file alone drops that spend
entirely: measured here, 2.70B tokens over 30 days, 16% of the real total. Those files
are read with the session and fold into its report, because the directory is the parent's
id and a subagent is not a session the platform could resolve.

Cumulative + upsert is the whole design. Hooks fire an unpredictable number of times,
are retried, and can miss a turn entirely; sending totals-so-far means the row converges
regardless, and neither side keeps a watermark that could be lost and re-counted.

**One API response is written to the transcript as several JSONL lines**, each repeating
the full `usage` block. Summing lines over-counts by 2–3×, so records are deduplicated
on `message.id` first — `calls` in a report is API responses, not lines. The dedup spans
the session's whole file set, not one file, so a response reaching both a subagent
transcript and the parent is still counted once.

## Running this on the same host as the platform

Safe, and deliberately so. **Only interactive sessions (`entrypoint: "cli"`) are
reported.** Anything driven by the Agent SDK (`entrypoint: "sdk-cli"`) belongs to
whatever program drove it, and that program is expected to meter itself — which is
exactly what the platform does for every run on its `claude-cli` provider. Reporting
those here would bill the same tokens twice: once from the platform's ledger write, once
from this transcript.

The platform also passes `persistSession: false`, so its runs leave no transcript to
find in the first place. That is a second, independent guard — but it is the platform's
own choice and could be revisited, so this filter is what makes the pair safe by
construction rather than by coincidence. The risk is not hypothetical: this machine
carries 1833 `sdk-cli` transcripts from an unrelated service, and a sweep without the
filter would hoover up every one of them.

A record whose `entrypoint` is missing or unrecognised is skipped, not reported:
under-reporting shows up in the numbers and can be re-run, double-reporting is silent
and corrupts the per-account view.

To include SDK-driven sessions anyway — a headless service on this host that meters
nothing itself:

```bash
AGENTS_USAGE_ENTRYPOINTS=cli,sdk-cli scripts/report-usage.sh --backfill 7
```

or `"entrypoints": ["cli", "sdk-cli"]` in the config file. Turn on `AGENTS_USAGE_DEBUG=1`
to see what each pass dropped — skips are logged, never silent.

Rows land as `kind: cli`, `provider: claude-cli`, cost **$0** (subscription quota, not a
bill), `conversationId` = the session id, and `accountId` = the login's email read from
`~/.claude.json`. They show up in the ledger and in the **By Claude account** breakdown.

Each report also names **where** and **from what**:

- `project` — the repo, as a path relative to your home (`acme/backend`).
  Resolved with `git worktree list`, not by taking the last path segment: a session
  started in `frontend/apps/storefront` belongs to its repo, not to `storefront`.
  Relative keeps your username out of the value and makes the same repo on two machines
  read as one project. A linked **worktree** folds into the checkout it was made from —
  a branch parked in `/tmp` is the same project as the repo in your home, not a second
  one under an absolute path. A **submodule** does not fold: it is its own repo and gets
  its own line. Outside a repo, the directory is used as-is.
- `device` — Claude Code's own `machineID` from `~/.claude.json`, truncated. It names a
  machine without carrying its hostname. Set `"device": "dev-pc"` in the config (or
  `AGENTS_USAGE_DEVICE`) for something a human can read. It is only a label: multi-device
  accounting is already correct without it, because the upsert key carries a session UUID
  that no two machines can collide on.

Still nothing else. The payload is under 300 bytes: session id, account, project, device,
and per-model token counts. No prompts, no answers, no file names, no tool names, no
branch. The transcript is read in full to count tokens and nothing from it is sent.

## Install

The marketplace manifest is at the repo root (`.claude-plugin/marketplace.json`), so this
repo IS the marketplace — there is nothing else to point at.

### From GitHub — any machine

```bash
claude plugin marketplace add vannguyen799/agents-usage
claude plugin install agents-usage@vt-plugins
```

Use `--scope local` (this repo only), `--scope project` (committed, for the team), or the
default `user` (this machine, every project). To pick up a later release:
`claude plugin marketplace update vt-plugins`, then restart Claude Code.

### From a local clone

The marketplace is registered as a **directory** pointing straight at the working tree —
it is not copied, so `git pull` updates the plugin:

```bash
claude plugin marketplace add ~/agents-usage
claude plugin install agents-usage@vt-plugins
```

### Without the plugin system at all

The hooks are two lines of settings. Point them at the script in your clone:

```jsonc
// ~/.claude/settings.json
{
  "hooks": {
    "Stop":       [{ "hooks": [{ "type": "command", "timeout": 10,
      "command": "/home/you/agents-usage/scripts/report-usage.sh" }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command", "timeout": 10,
      "command": "/home/you/agents-usage/scripts/report-usage.sh" }] }]
  }
}
```

## Configure

Nothing is reported until a URL and a token are present — with neither, every hook exits
0 in silence, which is the safe default for a plugin installed machine-wide.

```bash
cat > ~/.claude/agents-usage.json <<'JSON'
{
  "url": "http://127.0.0.1:16517",
  "token": "apk_...",
  "enabled": true
}
JSON
chmod 600 ~/.claude/agents-usage.json
```

Mint the token in the platform UI (**Settings → API tokens**). It needs `usage:write`,
which means role **editor** or above.

**Create it INSIDE a space.** A token belonging to no space resolves with far wider
rights than reporting usage needs, and this credential sits in a plain file on a laptop.
One created inside a space is confined to it, carries `usage:write` from the editor role
and nothing else, and its rows are attributed to that space. Give it a space of
its own rather than a project's — this is workstation spend, not the project's.

Environment variables override the file, so one shell can be pointed elsewhere for a
test: `AGENTS_USAGE_URL`, `AGENTS_USAGE_TOKEN`, `AGENTS_USAGE_ENABLED=0`,
`AGENTS_USAGE_ENTRYPOINTS`, `AGENTS_USAGE_PYTHON`, `AGENTS_USAGE_DEBUG=1`.

## Check it works

Print what would be sent, without sending anything:

```bash
echo '{"transcript_path":"'"$HOME"'/.claude/projects/<project>/<session>.jsonl"}' \
  | scripts/report_usage.py --print
```

Turn on the log (`~/.claude/agents-usage.log`) and watch a real hook:

```bash
AGENTS_USAGE_DEBUG=1 claude
```

Catch up sessions that ended without a final hook — a crash, a killed terminal. Safe to
re-run as often as you like, because the report is cumulative and the server upserts:

```bash
scripts/report-usage.sh --backfill 7    # last 7 days
```

## Turning it off

`AGENTS_USAGE_ENABLED=0`, or `"enabled": false` in the config, or
`claude plugin disable agents-usage`. Removing the config file stops it too.

## Requirements

`python3` (standard library only). No `jq`, no `curl`, no pip installs. Without it the
hook exits 0 and reports nothing.
