// agents-usage — reports each OpenCode session's token usage to the agent platform.
//
// OpenCode 2.x loads this from ~/.config/opencode/plugin/ (scripts/install.sh links
// it there). It does ONE thing: when a session stops running, start
// scripts/report-opencode-usage.sh for it, detached, and forget about it. Nothing
// here counts a token — the reporter reads OpenCode's own database.
//
// Un-failable, like the shell hooks the other two CLIs run: every path is caught,
// the child is detached with no stdio and never waited on, and a reporter that is
// missing is an error event on a child nobody listens to. A session never waits on
// this and never sees it fail.
import { spawn } from "node:child_process"
import { realpathSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

// "A run of this session just ended." 2.0.29 hands a plugin the execution's outcome
// as `{type, data: {sessionID}}` — checked against a live server, which delivered
// `session.execution.failed` for a prompt that could not run. The 1.x-era
// `session.idle` / `session.status` (idle) are not delivered to 2.x plugins and are
// accepted only in case a later release brings them back. Hearing several for one
// run is harmless — see DEBOUNCE_MS.
const ENDED = new Set([
  "session.execution.succeeded",
  "session.execution.failed",
  "session.execution.interrupted",
  "session.idle",
  "session.status",
])

// End events for one session inside this window start ONE report. The report is
// cumulative, so a merged pair loses nothing; it only saves a process per duplicate.
const DEBOUNCE_MS = 2000

// Resolved through the link back to the checkout it lives in, so this plugin and the
// reporter it starts always come from the same copy — and a `git pull` there updates
// both at once.
function reporterPath() {
  const here = realpathSync(fileURLToPath(import.meta.url))
  return join(dirname(dirname(here)), "scripts", "report-opencode-usage.sh")
}

function sessionOf(event) {
  if (!event || typeof event !== "object" || !ENDED.has(event.type)) return ""
  const data = event.data ?? event.properties ?? {}
  if (event.type === "session.status" && data.status?.type !== "idle") return ""
  const id = data.sessionID ?? data.sessionId
  // Passed as an argv entry, never through a shell — this only keeps a value that
  // could read as a flag from reaching the reporter as one.
  return typeof id === "string" && /^[A-Za-z0-9_]{1,128}$/.test(id) ? id : ""
}

export default {
  id: "agents-usage",
  setup(api) {
    let script
    try {
      script = reporterPath()
    } catch {
      return
    }

    const report = (id) => {
      try {
        const child = spawn("sh", [script, "--session", id], { detached: true, stdio: "ignore" })
        child.on("error", () => {})
        child.unref()
      } catch {}
    }

    const timers = new Map()
    const schedule = (id) => {
      clearTimeout(timers.get(id))
      const timer = setTimeout(() => {
        timers.delete(id)
        report(id)
      }, DEBOUNCE_MS)
      timer.unref?.()
      timers.set(id, timer)
    }

    const abort = new AbortController()
    ;(async () => {
      for await (const event of api.event.subscribe({ signal: abort.signal })) {
        try {
          const id = sessionOf(event)
          if (id) schedule(id)
        } catch {}
      }
    })().catch(() => {})

    // Unloaded (a reload, the service stopping): report what was still waiting
    // rather than drop it — that run's end is the one most likely to be its last.
    return () => {
      abort.abort()
      for (const [id, timer] of timers) {
        clearTimeout(timer)
        report(id)
      }
      timers.clear()
    }
  },
}
