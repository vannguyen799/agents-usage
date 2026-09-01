# agents-usage

Reports **this machine's Claude Code and Codex CLI token usage** into the agent
platform's usage ledger, so `/usage` shows a subscription's whole quota burn — not
just the half the platform spent itself.

The platform meters agents that run on its `claude-cli` and `codex-cli` providers. A
human typing into either CLI on the same login spends the same Pro/Max/Plus quota and
the platform sees none of it. This plugin closes that gap, for both, from one install.

## Install

```bash
scripts/install.sh
```

It asks which CLI to report from, what URL and token to use (offering whatever a
previous install already wrote), and then registers the marketplace **with updates
switched on** and installs the plugin. Re-running it is how you change one answer:
everything is a no-op when it is already done, and answering "keep" to the token
question leaves the one on disk untouched.

Unattended:

```bash
scripts/install.sh --platform both --url https://… --token aur_… --device dev-pc --yes
```

`--platform claude|codex|both` · `--source github|local` · `--device LABEL` · `--yes`.

### By hand

The marketplace manifests are at the repo root — this repo IS the marketplace, for
both CLIs, and there is nothing else to point at.

```bash
# Claude Code
claude plugin marketplace add vannguyen799/agents-usage
claude plugin install agents-usage@vt-plugins

# Codex
codex plugin marketplace add vannguyen799/agents-usage
codex plugin add agents-usage@vt-plugins
```

Claude Code takes `--scope local` (this repo only), `--scope project` (committed, for
the team) or the default `user` (this machine, every project). **Updates are not
automatic unless you ask for them**: `"autoUpdate": true` on the marketplace entry in
`~/.claude/settings.json` — which is what `install.sh` sets, and what
`claude plugin marketplace update vt-plugins` does by hand. Codex refreshes configured
Git marketplaces itself at startup; `codex plugin marketplace upgrade` forces it. Both
need the CLI restarted afterwards, and **Codex asks once to trust this plugin's
hooks** — until you say yes, nothing is reported.

A LOCAL Codex marketplace (`--source local`) is COPIED into `~/.codex/plugins/cache`
at install and does not follow a `git pull`. Claude Code's directory marketplace reads
the working tree, so there it does.

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

The same shape works in `~/.codex/hooks.json` with `report-codex-usage.sh`.

## Configure

Nothing is reported until a URL and a token are present — with neither, every hook
exits 0 in silence, which is the safe default for a plugin installed machine-wide.

```bash
cat > ~/.claude/agents-usage.json <<'JSON'
{
  "url": "http://127.0.0.1:16517",
  "token": "aur_...",
  "enabled": true
}
JSON
chmod 600 ~/.claude/agents-usage.json
```

ONE file serves both reporters — the credential belongs to the platform, not to a CLI
— and each looks in its own CLI's directory first: `~/.codex/agents-usage.json` then
`~/.claude/agents-usage.json` for Codex, the other way round for Claude Code. A machine
with both CLIs therefore needs only one file, and installing Codex on a machine that
still carries a stale Claude Code config reports with the token just written rather
than silently with the old one. `AGENTS_USAGE_CONFIG` overrides both.

Mint the key in the platform UI, on **/usage → "Report a machine"** — the panel also
prints the whole install as one line with the secret already in it. Anyone signed in
may mint one for their own machine.

What it mints is a **reporting key** (`aur_…`) whose entire vocabulary is
`usage:write`: it belongs to no space, adds usage rows and can read nothing back —
not an agent, not a run, not the ledger it writes to. That is what this credential
should be worth, because it sits in a plain file on a laptop. Usage reads are not
space-filtered, so a space never scoped this data; it only ever scoped the CREDENTIAL,
and it did that badly — `editor`, the narrowest space role that can report, also
writes agents, capabilities and knowledge.

A space-scoped `apk_…` token minted before that panel existed **keeps reporting**;
nothing needs re-installing.

Environment variables override the file, so one shell can be pointed elsewhere for a
test: `AGENTS_USAGE_URL`, `AGENTS_USAGE_TOKEN`, `AGENTS_USAGE_ENABLED=0`,
`AGENTS_USAGE_CONFIG`, `AGENTS_USAGE_ENTRYPOINTS`, `AGENTS_USAGE_ORIGINATORS`,
`AGENTS_USAGE_DEVICE`, `AGENTS_USAGE_PYTHON`, `AGENTS_USAGE_DEBUG=1`.

### Name this machine

Every row carries a `device`, so /usage can split one login's quota by the box that
spent it. By default that is **`user@host`** — what `whoami` and `hostname` say, with
the domain dropped. Give it a name of your own instead:

```bash
scripts/report_usage.py --set-device dev-pc   # name it
scripts/report_usage.py --set-device          # say what it is now, change nothing
scripts/report_usage.py --set-device --id     # use this machine's opaque id instead
scripts/report_usage.py --set-device ''       # back to user@host
```

It writes `device` into the config file both reporters read, so naming the machine
once names it for every CLI on it — `report_codex_usage.py` takes the same flag and
answers the same. `AGENTS_USAGE_DEVICE` still wins over the file, for one shell.

`--id` is for a box that must not report its login and hostname. The id is this
plugin's own, kept as `deviceId` in that same shared config — deliberately not Claude
Code's `machineID` or Codex's `installation_id`, which name the same laptop
differently per harness and do not exist at all on a harness that keeps neither. It is
also what a machine falls back to when it can name neither its user nor its host.

**It follows the device, not the login.** The id is derived (SHA-256 with an
app-specific salt, so the raw value is never sent) from the operating system's own
machine id — `/etc/machine-id`, macOS's IOPlatformUUID, Windows' MachineGuid. Two
things follow that a stored random id could not give you: deleting the config and
installing again re-derives the **same** id instead of inventing a second device, and
a second OS user on the same workstation derives it too rather than showing up as
another machine. Who spent the quota is already its own field (`accountId`), so two
people sharing a box stay apart by account while the box stays one row. An id already
in the config wins over deriving one, so a machine that has reported keeps the
identity it reported under; a box whose OS will not name itself (a container) gets a
random id, and that one is only used if it can actually be stored — a fresh id per
session would file every session as a separate device.

## How it works

Two hooks (`Stop`, `SessionEnd`) in each CLI run one script. It re-reads the session
transcript, sums tokens per model, and POSTs the **cumulative** total to
`POST /api/v1/usage/sessions`. The server upserts on `cli:<sessionId>:<model>`.

Cumulative + upsert is the whole design. Hooks fire an unpredictable number of times,
are retried, and can miss a turn entirely; sending totals-so-far means the row
converges regardless, and neither side keeps a watermark that could be lost and
re-counted.

Rows land as `kind: cli`, `provider: claude-cli` or `codex-cli`, cost **$0**
(subscription quota, not a bill), `conversationId` = the session id, and `accountId` =
the login that spent it. They show up in the ledger and in the **By CLI account**
breakdown.

Token counts are sent **split by rate class** — uncached input, cache read, 5-minute
cache write, 1-hour cache write, output — never folded into one figure. They are not
interchangeable: a cache read counts for a TENTH of an uncached token, a 1-hour cache
write for TWO, and output for FIVE on Claude and EIGHT on GPT-5. A long session is
~98% cache reads, so one folded number reads about seven times heavier than the work
actually was, and nothing can take it back apart afterwards. The server weights them
into `billable_tokens`; this only has to keep them separate.

### Claude Code

**Subagents and workflow agents count too.** Claude Code writes what the Task tool
spawns beside the session rather than into it — `<project>/<session-id>/subagents/` for
subagents, another two levels down under `subagents/workflows/wf_*/` for the agents a
workflow runs — and none of it reaches the parent transcript, so reading the session
file alone drops that spend entirely: measured here, 2.87B tokens over 30 days, 17% of
the real total. Everything under the session's directory is swept recursively and folds
into its report, because the directory is the contract — anything filed under a session
id was spent by that session, including a nesting level that does not exist yet.

**One API response is written to the transcript as several JSONL lines**, each
repeating the full `usage` block. Summing lines over-counts by 2–3×, so records are
deduplicated on `message.id` first — `calls` in a report is API responses, not lines.
The dedup spans the session's whole file set, not one file, so a response reaching both
a subagent transcript and the parent is still counted once.

### Codex

Codex records a running **`total_token_usage`** in its rollout instead of a per-response
block, which changes three things and every one of them is silent when got wrong:

- **The events are differenced, not added.** Summing them multiplies a session by its
  own turn count. The deltas always add back up to the last reading — including across
  a **compaction**, which zeroes the counter mid-session (6 of 324 rollouts here): the
  new reading is then taken whole rather than dropped, since the turn that re-reads the
  summary into an empty context is an expensive one to lose.
- **`input_tokens` INCLUDES `cached_input_tokens`**, where Anthropic reports them
  apart. The cached half is subtracted back out and sent as `cacheReadTokens`; passed
  through, it would be weighed as uncached input at ten times its cost, and a Codex
  session is almost all cache reads — 95.7% of the 904M input tokens across the 316
  rollouts this was measured on.
- **`reasoning_output_tokens` is a SUBSET of `output_tokens`**, not a fourth class.
  Adding it counts the thinking twice.

The model comes from each `turn_context`, so a session that changed model mid-way
splits across two rows instead of crediting all of it to whichever one finished.
`python3 scripts/smoke_codex_tally.py` pins all of the above against synthetic
rollouts; the same arithmetic was checked against all 324 real ones on the machine it
was written on.

## Running this on the same host as the platform

Safe, and deliberately so. **Only interactive sessions are reported** — `entrypoint:
"cli"` in Claude Code, `originator: "codex-tui"` in Codex. Anything driven by the Agent
SDK (`sdk-cli`) or by `codex exec` belongs to whatever program drove it, and that
program is expected to meter itself — which is exactly what the platform does for every
run on its `claude-cli` and `codex-cli` providers. Reporting those here would bill the
same tokens twice: once from the platform's ledger write, once from this transcript.

The platform also passes `persistSession: false` to Claude Code and `--ephemeral` to
Codex, so its runs leave no transcript to find in the first place. That is a second,
independent guard — but it is the platform's own choice and could be revisited, so
these filters are what make the pair safe by construction rather than by coincidence.
The risk is not hypothetical: this machine carries 1833 `sdk-cli` transcripts from an
unrelated service, and a sweep without the filter would hoover up every one of them.

A record whose origin is missing or unrecognised is skipped, not reported:
under-reporting shows up in the numbers and can be re-run, double-reporting is silent
and corrupts the per-account view.

To include those anyway — a headless service on this host that meters nothing itself:

```bash
AGENTS_USAGE_ENTRYPOINTS=cli,sdk-cli scripts/report-usage.sh --backfill 7
AGENTS_USAGE_ORIGINATORS=codex-tui,codex-exec scripts/report-codex-usage.sh --backfill 7
```

or `"entrypoints": ["cli", "sdk-cli"]` / `"originators": [...]` in the config file. Turn
on `AGENTS_USAGE_DEBUG=1` to see what each pass dropped — skips are logged, never
silent.

## What each report names

- `project` — the repo as `owner/repo` (`acme/backend`). The remote is the repo's real
  name, so one project reads the same on every machine and in every checkout, however
  the directory was named and wherever it was cloned. A session started in
  `frontend/apps/storefront` belongs to its repo, not to `storefront`; a linked
  **worktree** folds into the repo it was made from, and a **submodule** does not fold
  — it has its own `origin` and gets its own line. Claude Code's reporter asks git for
  it; Codex records the remote in the rollout itself, so a backfill can name a repo
  whose checkout has since moved. Only the URL's **path** is used, never its authority:
  a remote that carries a credential (`https://x-access-token:ghp_…@github.com/o/r`,
  which credential helpers and CI jobs write routinely) cannot leak it into the label,
  because the half a secret lives in is dropped before anything is parsed. With no
  `origin` to read, it falls back to the path relative to your home, which keeps your
  username out of the value.
- `device` — `user@host` by default: this machine's login and short hostname, which is
  the one identifier the person reading /usage already knows the box by. Both reporters
  derive it from the same function, so one laptop stays one row whichever CLI spent the
  quota — where a harness's own id (Claude Code's `machineID`, Codex's
  `installation_id`) would name it differently per CLI, and not at all on a harness
  that keeps none. `--set-device NAME` sends a chosen name instead, `--set-device --id`
  an opaque id this plugin derives from the OS's own machine id — one value for every
  CLI, every OS user and every reinstall on that box. It is
  only a label either way: multi-device accounting is already correct without it,
  because the upsert key carries a session UUID that no two machines can collide on.
- `accountId` — the Claude login's e-mail from `~/.claude.json`, or the ChatGPT
  `account_id` from `~/.codex/auth.json`. The Codex e-mail is not used: it exists only
  inside a JWT sitting next to a live refresh token, and a reporter that starts taking
  credential files apart for a nicer label is one edit away from sending the credential.
- `backend` — which endpoint served the session, because the server weights these
  tokens on the vendor's ratios and a partner backend (Bedrock, Vertex, a gateway, a
  proxy) is priced differently. Claude Code's transcript names none, so it is read from
  the environment — which works because the hook runs INSIDE the session it reports on.
  Codex records `model_provider` in the rollout, so that one survives a backfill. The
  base URL rides along sanitised (scheme, host, port and path only — never userinfo,
  query or fragment) so the server can CHECK the claim rather than take the name on
  trust.

Still nothing else. The payload is well under a kilobyte: session id, provider,
account, project, device, backend, and per-model token counts. No prompts, no answers,
no file names, no tool names, no branch. The transcript is read in full to count tokens
and nothing from it is sent.

The one identifier that grew is `device`: the default `user@host` carries this
machine's login and hostname, where the `machineID` it replaced carried neither. That
is the point of it — a fleet's rows are read by the person who owns the fleet — but a
box that should not say either can be given a name (`--set-device NAME`) or the opaque
id (`--set-device --id`).

## Check it works

Print what would be sent, without sending anything:

```bash
echo '{"transcript_path":"'"$HOME"'/.claude/projects/<project>/<session>.jsonl"}' \
  | scripts/report_usage.py --print
echo '{"transcript_path":"'"$HOME"'/.codex/sessions/2026/01/01/rollout-….jsonl"}' \
  | scripts/report_codex_usage.py --print
```

Turn on the log (`agents-usage.log`, beside the config) and watch a real hook:

```bash
AGENTS_USAGE_DEBUG=1 claude
AGENTS_USAGE_DEBUG=1 codex
```

Catch up sessions that ended without a final hook — a crash, a killed terminal. Safe to
re-run as often as you like, because the report is cumulative and the server upserts:

```bash
scripts/report-usage.sh --backfill 7          # Claude Code, last 7 days
scripts/report-codex-usage.sh --backfill 7    # Codex
```

And the arithmetic itself, plus the way this machine names itself:

```bash
python3 scripts/smoke_codex_tally.py
python3 scripts/smoke_device.py
```

## Turning it off

`AGENTS_USAGE_ENABLED=0`, or `"enabled": false` in the config, or
`claude plugin disable agents-usage` / `codex plugin remove agents-usage@vt-plugins`.
Removing the config file stops it too.

## Requirements

`python3` (standard library only). No `jq`, no `curl`, no pip installs. Without it the
hook exits 0 and reports nothing.
