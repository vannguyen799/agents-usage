# agents-usage

Reports **this machine's Claude Code, Codex CLI and OpenCode token usage** into the
agent platform's usage ledger, so `/usage` shows a subscription's whole quota burn —
not just the half the platform spent itself.

The platform meters agents that run on its `claude-cli` and `codex-cli` providers. A
human typing into either CLI on the same login spends the same Pro/Max/Plus quota and
the platform sees none of it — and so does OpenCode signed in to Anthropic or OpenAI.
This plugin closes that gap, for all three, from one install.

## Install

Nothing to clone — the installer is served raw off this repo:

```bash
curl -fsSL https://raw.githubusercontent.com/vannguyen799/agents-usage/refs/heads/main/scripts/install.sh \
  | sh -s -- --platform both --url https://… --token aur_… --yes
```

The platform's own **/usage → "Report a machine"** panel prints that line with the URL
and a freshly minted key already in it.

Piped, there is no terminal to ask into, so `--url` and `--token` must be flags — an
installer that reached the end without a key would leave a plugin that reports nothing
and never says why. From a clone it can ask instead:

```bash
scripts/install.sh
```

Either way it registers the marketplace **with updates switched on** and installs the
plugin. Re-running it is how you change one answer: everything is a no-op when it is
already done, and answering "keep" to the token question leaves the one on disk
untouched — which is also how a machine moves to a rotated key.

`--platform claude|codex|opencode` (comma-separated for several, e.g.
`claude,opencode`) · `--source github|local` · `--device LABEL` · `--yes`. `both` still
means `claude,codex`, exactly as before; `all` means every one of the three that is
installed on this machine, so one fleet command fits every box. `--source local`
installs the working tree instead of GitHub, so it needs the repo on disk and is
refused down a pipe.

### OpenCode

OpenCode (2.x) has no marketplace and no shell hooks: a plugin is a `.js` file in
`${XDG_CONFIG_HOME:-~/.config}/opencode/plugin/`, loaded by OpenCode's background
service. `install.sh --platform opencode` **links** `opencode/agents-usage.js` there
— a symlink, not a copy, because the plugin finds its reporter by following the link
back (`../scripts/report-opencode-usage.sh`), so whatever keeps that checkout current
updates both at once. Which checkout:

- `--source local` — this working tree; a `git pull` here is the update.
- `--source github` with Claude Code also installed — the marketplace clone Claude Code
  keeps current itself (`~/.claude/plugins/marketplaces/vt-plugins`, `autoUpdate`).
- `--source github` without it — a clone of its own in
  `${XDG_DATA_HOME:-~/.local/share}/agents-usage`, which re-running the installer pulls.

The running service picks the file up by itself — no restart, nothing interrupted —
and `opencode plugin list` shows `agents-usage  local`. Nothing else in
`~/.config/opencode/` is touched — save `agents-usage.json` itself, on a machine where
OpenCode is the only CLI and no config exists yet.

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

The same shape works in `~/.codex/hooks.json` with `report-codex-usage.sh`. For
OpenCode it is one link:

```bash
mkdir -p ~/.config/opencode/plugin
ln -s /home/you/agents-usage/opencode/agents-usage.js ~/.config/opencode/plugin/
```

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

ONE file serves every reporter — the credential belongs to the platform, not to a CLI
— and each looks in its own CLI's directory first: `~/.codex/agents-usage.json` then
`~/.claude/agents-usage.json` for Codex, the other way round for Claude Code, and
`${XDG_CONFIG_HOME:-~/.config}/opencode/agents-usage.json` first for OpenCode (then
the other two; the others try it last). A machine with several CLIs therefore needs
only one file, and installing Codex on a machine that still carries a stale Claude
Code config reports with the token just written rather than silently with the old
one. `AGENTS_USAGE_CONFIG` overrides all of them. Re-running the installer for one
more CLI while answering "keep" to the token edits the file that already holds it —
and leaves it alone entirely when no answer changed.

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
`AGENTS_USAGE_DEVICE`, `AGENTS_USAGE_PYTHON`, `AGENTS_USAGE_DEBUG=1`,
`AGENTS_USAGE_STATE_DIR` (default `~/.cache/agents-usage`), `AGENTS_USAGE_SYNC=1` (run
the report in the foreground, for debugging).

**The URL must be `https://`.** Plain `http://` is accepted only for `localhost` /
`127.0.0.1` / `[::1]`: the key travels in every report's Authorization header. Both
`install.sh` and the reporters refuse anything else, and the reporters never follow a
redirect (urllib would turn a redirected POST into a GET that re-sends the header).

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

It writes `device` into the config file every reporter reads, so naming the machine
once names it for every CLI on it — `report_codex_usage.py` and
`report_opencode_usage.py` take the same flag and answer the same. `AGENTS_USAGE_DEVICE` still wins over the file, for one shell.

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

Two hooks (`Stop`, `SessionEnd`) in Claude Code and Codex run one script; in OpenCode
the plugin starts it when a session's run ends. It re-reads the session, sums tokens
per model **and minute**, and POSTs the **cumulative** total to
`POST /api/v1/usage/sessions`. The server upserts on `cli:<sessionId>:<model>:<minute>`.

Cumulative + upsert is the whole design. Hooks fire an unpredictable number of times,
are retried, and can miss a turn entirely; sending totals-so-far means the row
converges regardless, and neither side keeps a watermark that could be lost and
re-counted.

### In the background, and only what is new

The hook itself only queues its payload and starts a detached worker, so the session
never waits on a report. One worker runs at a time per CLI (`flock -n` on
`<state>/<cli>/worker.lock`); a hook that finds it busy leaves its payload in the queue
and the running worker drains it before it exits.

The Claude Code worker keeps a byte offset per transcript file (and per subagent file)
in `<state>/claude/state/`, and parses only the lines written since the last run. The
OpenCode worker keeps, in `<state>/opencode/state/`, the newest `time_updated` read per
session and each counted message's own contribution (see below). What
it keeps is the session's FULL running total per (model, minute), and it sends only the
minutes whose total changed — still as totals, never deltas, so the upsert converges
exactly as before. Deleting the state dir costs one full re-read and re-send, nothing
else. A report the platform cannot take (no answer, 5xx, 429, an expired key) is kept
in `<state>/<cli>/spool/`, merged with anything already spooled for that session, and
sent on a later run; spooled reports expire after 14 days.

### The hour (0.8.0)

A session is a **span**, not an instant. One left open all morning is a single session,
and reported as a single row it can only be charted as an even smear across every hour
it was alive — a flat line where the real spend was two bursts and a long idle. That is
what /usage looked like before 0.8.0.

Both transcripts already stamp every response, so nothing has to be measured — only
kept. Each response is filed under the UTC hour of its own stamp, and a report carries
one entry per (model, hour):

```json
{ "model": "claude-opus-5", "bucket": "2026-09-03T07:00:00Z",
  "startedAt": "2026-09-03T07:05:17.365Z", "endedAt": "2026-09-03T07:59:35.661Z",
  "promptTokens": 302, "completionTokens": 132322, "cacheReadTokens": 30891972,
  "cacheWrite5mTokens": 0, "cacheWrite1hTokens": 267496, "calls": 151 }
```

`bucket` is half the upsert key, so the report stays idempotent at the finer
resolution — re-reading the same transcript rewrites the same rows. `startedAt` /
`endedAt` are the first and last response inside that hour, which is what a chart finer
than an hour has to draw from; the server clamps them into the bucket rather than
trusting a clock it does not run.

UTC, deliberately: the hour is part of a key, so it has to mean the same thing on a
laptop that travels. The /usage chart cuts its own buckets in the reader's zone, from
the stamps, never from this.

A record whose stamp cannot be read inherits the hour of the one before it — a
transcript is only ever appended to. A transcript with no usable stamp **anywhere**
sends no `bucket` at all and gets the pre-0.8.0 whole-session row, which every server
still handles. What a report may never do is mix the two: that would write both a
per-hour row and a whole-session row for one model, the same tokens twice, and the
server refuses it.

**Upgrading mid-session is handled.** A session live across the upgrade has a
whole-session row from before it and per-hour rows from after — the first bucketed
report deletes the old one, since the finer rows say everything it said.

### The minute (0.9.0)

The hour fixed the day chart and not the minute one: a row holding every response
from 07:05 to 07:58 still had to be spread evenly across those 53 minutes, so /usage
drew a flat line inside every busy hour. So 0.9.0 files each response under its UTC
**minute** — the finest bucket /usage draws — and says so on the report with
`"bucketSize": "minute"`; `bucket` becomes `2026-09-03T07:05:00Z`. A minute row lands
whole in one chart bucket at every zoom, which is the picture a row per API call would
draw at a quarter of the rows.

A server older than 0.9.0 ignores `bucketSize`, floors each minute to its hour and folds
them — the 0.8.x rows, exactly. A 0.9.0 server keys minutes apart from hours
(`…T07:05` against `…T07`) and deletes the hour rows a minute report replaces, so a
session live across the upgrade is not counted twice — and the minute rows an hour
report replaces, for a session still running its 0.8.x hook after a 0.9.0 backfill.

A report never carries more than 1,024 entries (the server's cap — about 17 busy hours
on one model; the largest session measured had 262). Past it only the newest WHOLE
hours are sent: the older minutes are final and went up with earlier reports.

Sessions reported by 0.8.x stay hour-shaped until they report again; `--backfill
<days>` re-reads recent transcripts and converts them.

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

### OpenCode

OpenCode 2.x keeps every session in ONE SQLite database —
`${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db` — which the reporter opens
**read-only** (`mode=ro`; it is in WAL mode, so reading beside the live service blocks
nothing). Spend is on two message types in `session_message`: `assistant` (one model
response, `data.model` + `data.tokens`) and `compaction` (the summary call). Its
token block is not shaped like either vendor's, and each difference is silent when
got wrong — read out of the 2.0.29 binary's own normaliser and then checked against
the cost OpenCode recorded on 153 real Anthropic messages, which a fit reproduces to
the cent:

- **`output` EXCLUDES `reasoning`** — the opposite of Codex. OpenCode stores
  `output - reasoning` as visible output and prices `output + reasoning` at the
  output rate (the fit puts both at the same price, as two separate addends), so the
  ledger's `completionTokens` is the SUM. Taking `output` alone dropped 30% of one
  real session's output.
- **`input` excludes both cache classes for every provider.** OpenCode has already
  taken OpenAI's cached prefix out of `input_tokens`, so nothing is subtracted here —
  doing what the Codex reporter does would take it out twice.
- **`cache.write` is a 5-minute write on Anthropic.** OpenCode's default cache hint
  names no TTL, and the recorded costs price the writes at exactly 1.25x input. On
  OpenAI a write is billed as input and is folded into `promptTokens`, as Codex's is.
- **The provider decides the row.** `providerID` `anthropic` is filed as `claude-cli`,
  `openai` as `codex-cli` — the subscriptions they spend. Anything else (OpenCode's
  own free models, OpenRouter, an API-key gateway) is skipped and logged under
  `AGENTS_USAGE_DEBUG`. A session that switched providers sends one body per provider.

The session tree:

- **Subagents fold into their root**, like Claude Code's. A child session names its
  parent in `parent_id`; a hook for any session in the tree reports the whole tree
  under the root's id, so a child's spend is filed under exactly one id. A child is
  never reported under its own.
- **A fork does not re-count what it inherited.** Forking COPIES the source session's
  messages — tokens included — into the new one, keeping each copy's original
  `time_created`; OpenCode starts the fork's own totals at zero, and so does this, by
  skipping every row older than the session it sits in.
- **Rows are rewritten in place** while a response streams, so the worker re-reads by
  `time_updated` and REPLACES a message's contribution by its id. Keeping
  contributions also means a reverted (deleted) message stays counted: its quota was
  spent either way. Every message is filed under the minute it was created, which
  never moves.
- **Compaction counts**, on the model it names; a failed one names none and counts
  on the session's model, which is the model it ran on. **Title generation does
  not**: OpenCode folds it into the session's totals with no model recorded anywhere —
  roughly a thousand tokens a session or less, mostly cache, measured here.

`python3 scripts/smoke_opencode_tally.py` pins all of the above against a synthetic
database.

## Running this on the same host as the platform

Safe, and deliberately so. **Only interactive sessions are reported** — `entrypoint:
"cli"` in Claude Code, `originator: "codex-tui"` in Codex. (OpenCode is the exception;
see below.) Anything driven by the Agent
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

**OpenCode sessions are reported however they were started** — no origin filter.
OpenCode records no equivalent of `entrypoint`, and the one program on this side that
drives it, agent-tool-gateway (over `opencode acp`), meters nothing to the platform
itself, so there is no second ledger write to double. The platform has no OpenCode
provider. If something that DOES meter itself ever starts driving OpenCode on a
reporting machine, this is the place that needs a filter.

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
  `account_id` from `~/.codex/auth.json`. For OpenCode, the e-mail of the OpenCode
  login (the `account` table's `email` column, nothing else from it — the same table
  holds that login's tokens, and `credential` the provider keys; neither is read).
  That names who is signed in to OpenCode, not which Anthropic or OpenAI subscription
  the provider key belongs to, and it is empty when nobody is signed in. The Codex e-mail is not used: it exists only
  inside a JWT sitting next to a live refresh token, and a reporter that starts taking
  credential files apart for a nicer label is one edit away from sending the credential.
- `backend` — which endpoint served the session, because the server weights these
  tokens on the vendor's ratios and a partner backend (Bedrock, Vertex, a gateway, a
  proxy) is priced differently. Claude Code's transcript names none, so it is read from
  the environment — which works because the hook runs INSIDE the session it reports on.
  Codex records `model_provider` in the rollout, so that one survives a backfill.
  OpenCode's is `firstParty` unless `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` in the
  service's environment, or a `baseURL` in the global OpenCode config, points the
  provider elsewhere. The base URL rides along sanitised (scheme, host, port and path only — never userinfo,
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
scripts/report_opencode_usage.py --print                       # the latest session
scripts/report_opencode_usage.py --print --session ses_…       # a given one (or its subagent)
```

Turn on the log (`agents-usage.log`, beside the config) and watch a real hook:

```bash
AGENTS_USAGE_DEBUG=1 claude
AGENTS_USAGE_DEBUG=1 codex
AGENTS_USAGE_DEBUG=1 opencode --standalone   # a private server, so the plugin inherits it
AGENTS_USAGE_DEBUG=1 AGENTS_USAGE_SYNC=1 scripts/report-opencode-usage.sh --session ses_…
```

OpenCode's plugin runs inside its BACKGROUND service, so the variable has to be in
that service's environment, not the terminal's — hence `--standalone`, or the last
line, which reports one session in the foreground exactly as the plugin would.

Catch up sessions that ended without a final hook — a crash, a killed terminal. Safe to
re-run as often as you like, because the report is cumulative and the server upserts:

```bash
scripts/report-usage.sh --backfill 7          # Claude Code, last 7 days
scripts/report-codex-usage.sh --backfill 7    # Codex
scripts/report-opencode-usage.sh --backfill 7 # OpenCode
```

And the arithmetic itself, plus the way this machine names itself:

```bash
python3 scripts/smoke_transcript_tally.py   # Claude Code: dedup, subagents, the hour
python3 scripts/smoke_codex_tally.py        # Codex: deltas, compaction, the hour
python3 scripts/smoke_opencode_tally.py     # OpenCode: classes, providers, subagents, forks
python3 scripts/smoke_device.py             # how this machine names itself
```

## Turning it off

`AGENTS_USAGE_ENABLED=0`, or `"enabled": false` in the config, or
`claude plugin disable agents-usage` / `codex plugin remove agents-usage@vt-plugins` /
`rm ~/.config/opencode/plugin/agents-usage.js`. Removing the config file stops it too.

## Requirements

`python3` (standard library only — OpenCode's database is read with its `sqlite3`
module). No `jq`, no `curl`, no pip installs. Without it the hook exits 0 and reports
nothing. OpenCode 2.x for the OpenCode plugin: it uses 2.x's plugin API
(`export default { id, setup }`), which 1.x does not load.
