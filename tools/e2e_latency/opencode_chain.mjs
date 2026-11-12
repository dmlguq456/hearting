// Measure the OpenCode plugin's hook callbacks directly (T4, no model call).
//
// Loads adapters/opencode/plugins/hearting-guards.js from --code-root, builds the
// plugin with a fake client (the same way hooks/session-card-inject.test.sh does),
// and awaits chat.message / tool.execute.before / tool.execute.after --reps times.
// For every await it records the awaited time and the child processes created while
// it ran: spawnSync children (they block the callback) versus spawn children (they run
// beside it). The caller supplies the isolated env (fixture HOME/XDG/PATH) and cwd.
//
//   node opencode_chain.mjs --code-root <root> --directory <project> [--reps 5] [--session S]
//
// Prints one JSON object on stdout.
import cp from "node:child_process"
import { syncBuiltinESMExports } from "node:module"
import path from "node:path"
import { pathToFileURL } from "node:url"

function parseArgs(argv) {
  const args = { reps: 5, session: "perf-target", drainMs: 8000 }
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i]
    const value = argv[i + 1]
    if (key === "--code-root") args.codeRoot = value
    else if (key === "--directory") args.directory = value
    else if (key === "--reps") args.reps = Math.max(1, Number.parseInt(value, 10) || 1)
    else if (key === "--session") args.session = value
    else if (key === "--drain-ms") args.drainMs = Number.parseInt(value, 10) || args.drainMs
    else throw new Error(`unknown argument ${key}`)
  }
  if (!args.codeRoot || !args.directory) throw new Error("--code-root and --directory are required")
  return args
}

// The plugin imports { spawnSync, spawn } from "node:child_process". Replacing the CJS
// exports and syncing the ESM bindings lets us count children without editing the plugin.
let current = null
const originalSync = cp.spawnSync
const originalSpawn = cp.spawn
const label = (command, args) => {
  const script = Array.isArray(args) ? args.find((item) => typeof item === "string" && /\.(py|sh|js)$/.test(item)) : ""
  return script ? `${path.basename(String(command))} ${path.basename(script)}` : path.basename(String(command))
}
cp.spawnSync = function patchedSpawnSync(command, args, options) {
  const started = performance.now()
  const result = originalSync.apply(this, arguments)
  if (current) current.sync.push({ command: label(command, args), ms: performance.now() - started })
  return result
}
cp.spawn = function patchedSpawn(command, args, options) {
  const child = originalSpawn.apply(this, arguments)
  if (current) {
    current.detached.push({ command: label(command, args) })
    current.pending.push(new Promise((resolve) => {
      child.once("exit", resolve)
      child.once("error", resolve)
    }))
  }
  return child
}
syncBuiltinESMExports()

const median = (values) => {
  if (!values.length) return 0
  const sorted = [...values].sort((a, b) => a - b)
  const mid = sorted.length >> 1
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

async function timed(fn, drainMs) {
  current = { sync: [], detached: [], pending: [] }
  const frame = current
  const started = performance.now()
  let threw = ""
  try {
    await fn()
  } catch (error) {
    threw = String(error?.message || error).slice(0, 200)
  }
  const elapsed = performance.now() - started
  current = null
  const drainStarted = performance.now()
  await Promise.race([Promise.all(frame.pending), new Promise((resolve) => setTimeout(resolve, drainMs))])
  return {
    ms: elapsed,
    sync_children: frame.sync.length,
    sync_ms: frame.sync.reduce((sum, item) => sum + item.ms, 0),
    detached_children: frame.detached.length,
    drain_ms: performance.now() - drainStarted,
    commands: [...frame.sync.map((item) => `sync:${item.command}`),
      ...frame.detached.map((item) => `spawn:${item.command}`)],
    threw,
  }
}

const args = parseArgs(process.argv.slice(2))
const pluginPath = path.join(path.resolve(args.codeRoot), "adapters", "opencode", "plugins", "hearting-guards.js")
const importStarted = performance.now()
const { AgentHarnessGuards } = await import(pathToFileURL(pluginPath).href)
const plugin = await AgentHarnessGuards({
  directory: args.directory,
  worktree: args.directory,
  client: { session: {
    get: async (request) => ({ data: { id: request.path.id, parentID: null } }),
    messages: async () => ({ data: [] }),
  } },
})
const importMs = performance.now() - importStarted

const sid = args.session
const events = { "chat.message": [], "tool.execute.before": [], "tool.execute.after": [] }
for (let rep = 0; rep < args.reps; rep += 1) {
  const callID = `call-chain-${rep}`
  events["chat.message"].push(await timed(() => plugin["chat.message"](
    { sessionID: sid, messageID: `msg-chain-${rep}` },
    { parts: [{ type: "text", text: "latency measurement prompt: list the files" }], env: {}, message: {} },
  ), args.drainMs))
  events["tool.execute.before"].push(await timed(() => plugin["tool.execute.before"](
    { tool: "bash", sessionID: sid, callID },
    { args: { command: "true" } },
  ), args.drainMs))
  events["tool.execute.after"].push(await timed(() => plugin["tool.execute.after"](
    { tool: "bash", sessionID: sid, callID, args: { command: "true" } },
    { title: "bash", output: "", metadata: { exit: 0 } },
  ), args.drainMs))
}

const summary = {}
for (const [name, runs] of Object.entries(events)) {
  summary[name] = {
    runs,
    median_ms: median(runs.map((run) => run.ms)),
    sync_children_median: median(runs.map((run) => run.sync_children)),
    detached_children_median: median(runs.map((run) => run.detached_children)),
    threw: [...new Set(runs.map((run) => run.threw).filter(Boolean))],
  }
}
process.stdout.write(JSON.stringify({
  plugin: pluginPath, import_ms: importMs, reps: args.reps, session: sid, events: summary,
}) + "\n")
process.exit(0)
