import path from "node:path"
import { fileURLToPath } from "node:url"
import { spawnSync, spawn } from "node:child_process"
import { existsSync, mkdirSync, writeFileSync, utimesSync } from "node:fs"

const pluginDir = path.dirname(fileURLToPath(import.meta.url))
const pluginRoot = path.resolve(pluginDir, "../../..")
const envRoot = process.env.AGENT_HOME ? path.resolve(process.env.AGENT_HOME) : ""
const isHarnessRoot = (candidate) =>
  candidate &&
  existsSync(path.join(candidate, "core", "CORE.md")) &&
  existsSync(path.join(candidate, "adapters", "opencode", "bin", "preflight.sh"))
const root = isHarnessRoot(envRoot) ? envRoot : pluginRoot
const preflight = path.join(root, "adapters", "opencode", "bin", "preflight.sh")
const summaryTrigger = path.join(root, "utilities", "session_summary_trigger.py")
const designPattern = /(designs?\/|\/design\/|spec\/design|preview\.html$|slides?\.html$|03_components|scaffolds\/)/
// Capabilities that mutate the spec blueprint — must pass the prd.md read gate in a
// spec-backed cwd. Mirrors Claude's PreToolUse[Skill] spec-skill-gate scope.
const specGovernedCapabilities = new Set(["autopilot-code", "autopilot-spec"])
const promptBySession = new Map()
const turnBySession = new Map()
// Prompt-lifecycle context that must stay visible for every model call of a
// session, not just its first one. OpenCode has no Claude-style
// `additionalContext` that merges into the user turn and persists in history:
// `experimental.chat.system.transform` output lives only in the system prompt of
// the single request it decorates. Measured on opencode 1.17.13 — the transform
// fires once per model call (title generation, the answering turn, and every
// tool-loop continuation), so a once-per-session injection is consumed by the
// title call and never reaches the answering model at all.
//   * memoryBySession — session memory briefing, computed once per session and
//     re-emitted on every call so it persists the way Claude's SessionStart
//     additionalContext does.
//   * turnContextBySession — { turn, blocks } for the capsule candidate probe
//     and the per-turn signals: recomputed when a new user turn arrives, then
//     re-emitted on every model call of that turn.
const memoryBySession = new Map()
const turnContextBySession = new Map()

function baseDir(ctx) {
  return ctx.worktree || ctx.directory || process.cwd()
}

// Headless dispatch liveness probe support.
// When the OpenCode runtime starts a headless dispatch via dispatch-headless.py,
// it exports OPENCODE_DISPATCH_SLUG (and the dispatch interpreter passes the
// same env to the runtime child). Recording two artifacts at plugin init gives
// dispatch-liveness.py a secondary, cheap signal independent of the OpenCode
// SQLite session mtime:
//   * <agent-home>/.dispatch/plugin-load.<slug>.mark — created once at plugin
//     init, proving the plugin was actually loaded by the headless runtime.
//   * <agent-home>/.dispatch/logs/<slug>.heartbeat — touched on every
//     session.idle event (idle == turn done == still alive), so a stale or
//     crashed headless that never reaches idle will have an aging heartbeat.
// Both are best-effort: a plugin must never block a turn because it failed to
// record a liveness side-channel.
function dispatchSlug() {
  return process.env.OPENCODE_DISPATCH_SLUG || ""
}

function isWorkerSession() {
  return (
    (process.env.AGENT_SESSION_ROLE || "").toLowerCase() === "worker" ||
    process.env.AGENT_DISPATCH_CHILD === "1" ||
    Boolean(process.env.AGENT_DISPATCH_DEPTH) ||
    Boolean(process.env.OPENCODE_DISPATCH_SLUG) ||
    process.env.FLEET_TITLE_REFRESH === "1" ||
    process.env.MEM_DISTILL === "1"
  )
}

function touchHeartbeat(slug) {
  if (!slug) return
  try {
    const dispatchDir = path.join(root, ".dispatch")
    const logsDir = path.join(dispatchDir, "logs")
    mkdirSync(logsDir, { recursive: true })
    const hb = path.join(logsDir, `${slug}.heartbeat`)
    const now = new Date()
    try {
      utimesSync(hb, now, now)
    } catch {
      writeFileSync(hb, `${now.toISOString()}\n`, { encoding: "utf8" })
    }
  } catch {
    // best-effort; liveness side-channel must never throw
  }
}

function markPluginLoaded(slug) {
  if (!slug) return
  try {
    const dispatchDir = path.join(root, ".dispatch")
    mkdirSync(dispatchDir, { recursive: true })
    const marker = path.join(dispatchDir, `plugin-load.${slug}.mark`)
    writeFileSync(marker, `${new Date().toISOString()}\n`, { encoding: "utf8" })
  } catch {
    // best-effort
  }
}

function normalizeFile(ctx, file) {
  if (!file || file === "/dev/null") return ""
  if (path.isAbsolute(file)) return file
  return path.resolve(baseDir(ctx), file)
}

function patchFiles(ctx, patch) {
  if (!patch) return []
  const files = []
  const pattern = /^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$/gm
  let match
  while ((match = pattern.exec(patch)) !== null) {
    const file = normalizeFile(ctx, match[1] || match[2])
    if (file) files.push(file)
  }
  return files
}

function targetFiles(ctx, tool, args) {
  const name = typeof tool === "string" ? tool : tool?.name || ""
  if (name === "write" || name === "edit") {
    return [normalizeFile(ctx, args.filePath || args.path || args.file)].filter(Boolean)
  }
  if (name === "apply_patch" || name === "patch") {
    return patchFiles(ctx, args.patchText || args.patch || "")
  }
  return []
}

function isDesignHtml(file) {
  return /\.html?$/i.test(file) && designPattern.test(file.replaceAll(path.sep, "/"))
}

function runPreflight(command, args) {
  const result = spawnSync(preflight, [command, ...args], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    encoding: "utf8",
  })

  if (result.status !== 0) {
    const detail = [result.stdout, result.stderr].filter(Boolean).join("\n").trim()
    throw new Error(detail || `agent harness preflight failed: ${command}`)
  }
}

function runWorkerState(action, payload = {}) {
  const helper = path.join(root, "utilities", "worker-state-hook.py")
  const result = spawnSync("python3", [helper, action], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    input: JSON.stringify(payload),
    encoding: "utf8",
  })
  if (result.status !== 0) {
    const detail = [result.stdout, result.stderr].filter(Boolean).join("\n").trim()
    throw new Error(detail || `worker state hook failed: ${action}`)
  }
  return (result.stdout || "").trim()
}

function spawnDetached(command, args) {
  // Fire-and-forget: must not block the user's turn. The child runs the
  // preflight session-end → no-tools distiller worker independently.
  try {
    const child = spawn(preflight, [command, ...args], {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root },
      detached: true,
      stdio: "ignore",
    })
    child.unref()
  } catch {
    // best-effort; distillation is non-critical
  }
}

function spawnSummary(sid, phase) {
  if (!sid || isWorkerSession()) return
  try {
    const child = spawn("python3", [summaryTrigger, "--harness", "opencode",
      "--sid", sid, "--phase", phase, "--wait", phase === "initial" ? "5" : "1"], {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root, AGENT_SESSION_ROLE: "worker" },
      detached: true,
      stdio: "ignore",
    })
    child.unref()
  } catch {
    // best-effort; session execution never depends on observational summaries
  }
}

function collectPreflight(command, args) {
  const result = spawnSync(preflight, [command, ...args], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    encoding: "utf8",
  })

  return [result.stdout, result.stderr].filter(Boolean).join("\n").trim()
}

function collectCandidates(args) {
  const result = spawnSync(preflight, ["candidates", ...args], {
    cwd: root,
    env: { ...process.env, AGENT_HOME: root },
    encoding: "utf8",
    timeout: 3000,
    killSignal: "SIGKILL",
  })
  if (result.error || result.status !== 0) return ""
  return (result.stdout || "").trim()
}

// SD-111 P4 -- OpenCode carrier 2. Called only from "chat.message" (turn
// identity), never from "experimental.chat.system.transform" (which the
// header comment above documents as re-firing on every model call --
// title generation, the answering turn, and every tool-loop continuation).
// A22 asserts zero re-injection there; this function must never be called
// from that handler. OpenCode is measured `documented-only` for
// session-generation proof (§3.5), so the sweep this spawns is always
// refused with `pending-delivery-generation-unproven` -- fire-and-forget,
// fail-open, must never block or throw into the turn.
function sd111SessionSweep(sid) {
  if (!sid || isWorkerSession()) return
  const script = [
    "import sys",
    "sys.path.insert(0, sys.argv[2])",
    "try:",
    "    from dispatch_contract import dispatch_state_roots, resolve_agent_home",
    "    from dispatch_session_sweep import sweep",
    "    roots = dispatch_state_roots(resolve_agent_home())",
    "except Exception:",
    "    roots = ()",
    "for r in roots:",
    "    try:",
    "        sweep(r, 'opencode-turn', sys.argv[1], 'unsupported')",
    "    except Exception:",
    "        pass",
  ].join("\n")
  try {
    const child = spawn("python3", ["-c", script, sid, path.join(root, "utilities")], {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root },
      detached: true,
      stdio: "ignore",
    })
    child.unref()
  } catch {
    // best-effort; carrier 2 must never block a turn
  }
}

function appendContext(output, text) {
  if (!text) return
  if (!Array.isArray(output.system)) output.system = []
  output.system.push(text)
}

// F-100c -- receive side of a herdr steer, harness-neutral: a steward appends
// `(peer-from: <harness> <sid> <name>)` to the prompt body; the receiver writes its
// own `notice` peer_message_v1 under its exact session id (same record the Claude
// hook and the Codex hook write). Detached, fail-soft, never blocks the turn.
const peerTrailerRe = /\(peer-from:\s*([A-Za-z0-9_-]+)\s+([^\s)]+)(?:\s+([^)]*?))?\s*\)/g

function spawnPeerNotice(sid, prompt, cwd) {
  if (!sid || !prompt || !prompt.includes("peer-from:")) return
  let match = null
  for (const m of prompt.matchAll(peerTrailerRe)) match = m
  if (!match) return
  const [, fromHarness, fromSid, fromName] = match
  const tool = path.join(root, "utilities", "peer-message.py")
  const args = [tool, "record",
    "--from-harness", String(fromHarness).toLowerCase(),
    "--from-session-id", fromSid,
    "--from-project", path.basename(cwd || ""),
    "--to-harness", "opencode",
    "--to-session-id", sid,
    "--kind", "notice", "--surface", "herdr", "--status", "received",
    "--body-stdin"]
  if (fromName && fromName.trim()) args.push("--from-name", fromName.trim())
  try {
    const child = spawn("python3", args, {
      cwd: root,
      env: { ...process.env, AGENT_HOME: root },
      stdio: ["pipe", "ignore", "ignore"],
      detached: true,
    })
    child.on("error", () => {})
    child.stdin.end("herdr steer received")
    child.unref()
  } catch {}
}

function promptText(output) {
  if (typeof output?.message?.content === "string") return output.message.content
  if (!Array.isArray(output?.parts)) return ""
  return output.parts
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n")
}

export const AgentHarnessGuards = async (ctx) => {
  // Record plugin-load marker once per plugin init. In a headless dispatch the
  // runtime child inherits OPENCODE_DISPATCH_SLUG, so this proves the plugin
  // was loaded by the headless runtime (dispatch-liveness.py inspects it).
  markPluginLoaded(dispatchSlug())

  return ({
  event: async ({ event }) => {
    if (event && event.type === "session.compacted") {
      runWorkerState("compact-after", event)
    }
    // session.idle fires after each turn (the session is waiting for the user).
    // Use it as the auto-distillation trigger; preflight session-end debounces
    // per session and the --pure worker never re-enters this plugin. Mirrors the
    // Claude SessionEnd + codex session-end detached distiller.
    if (event && event.type === "session.idle") {
      const eventSid = (event.properties && event.properties.sessionID) || ""
      const sid = eventSid || "opencode-plugin"
      if (!isWorkerSession()) {
        spawnDetached("session-end", [baseDir(ctx), sid])
        spawnSummary(eventSid, "final")
      }
      // Liveness side-channel: touch the heartbeat for the active dispatch slug
      // so dispatch-liveness.py can detect stale/crashed headless sessions even
      // when the OpenCode SQLite session mtime is inconclusive.
      touchHeartbeat(dispatchSlug())
    }
    if (event && event.type === "session.deleted") {
      const sid = (event.properties && event.properties.sessionID) || ""
      if (sid) {
        spawnSummary(sid, "final")
        promptBySession.delete(sid)
        turnBySession.delete(sid)
        memoryBySession.delete(sid)
        turnContextBySession.delete(sid)
      }
    }
  },
  "chat.message": async (input, output) => {
    if (isWorkerSession()) return
    const eventSid = input.sessionID || output?.message?.sessionID || ""
    const sid = eventSid || "opencode-plugin"
    spawnSummary(eventSid, "initial")
    sd111SessionSweep(sid)
    const prompt = promptText(output)
    const turn = input.messageID || output?.message?.id || ""
    if (prompt) promptBySession.set(sid, prompt)
    if (prompt && eventSid) spawnPeerNotice(eventSid, prompt, baseDir(ctx))
    if (turn) turnBySession.set(sid, turn)
  },
  "experimental.chat.system.transform": async (input, output) => {
    const sid = input.sessionID || "opencode-plugin"
    const cwd = baseDir(ctx)
    if (isWorkerSession()) {
      // Dispatch prompts own explicit status/prompt-signal bootstrap;
      // memory/briefing/context stay main-only.
      return
    }
    // Every model call re-emits the same blocks. The probe/preflight work still
    // runs once per session (memory) or once per user turn (candidates,
    // prompt-signal, briefing) — only the emission repeats, so the caps in
    // core/MEMORY.md are unchanged and no extra process is spawned per
    // tool-loop continuation.
    if (!memoryBySession.has(sid)) {
      memoryBySession.set(sid, collectPreflight("memory", [cwd]))
    }
    appendContext(output, memoryBySession.get(sid))

    const prompt = promptBySession.get(sid) || ""
    const turn = turnBySession.get(sid) || ""
    const cached = turnContextBySession.get(sid)
    // A turn is new when chat.message recorded a prompt this plugin has not
    // built context for yet. Sessions whose runtime supplies no message ID fall
    // back to the prompt text itself as the turn key.
    const turnKey = turn || prompt
    if (prompt && (!cached || cached.turn !== turnKey)) {
      const blocks = [
        collectCandidates([prompt, cwd, sid, turn]),
        collectPreflight("local-evidence", [cwd]),
        collectPreflight("prompt-signal", [cwd, sid]),
        collectPreflight("briefing", [cwd]),
      ].filter(Boolean)
      turnContextBySession.set(sid, { turn: turnKey, blocks })
    }
    for (const block of turnContextBySession.get(sid)?.blocks || []) {
      appendContext(output, block)
    }
  },
  "experimental.session.compacting": async (input, output) => {
    runWorkerState("compact-before", input || {})
  },
  "command.execute.before": async (input, output) => {
    // Spec read gate — deny autopilot-code/spec in a spec-backed cwd until prd.md
    // was actually read this session. Mirrors Claude's PreToolUse[Skill] hard deny:
    // preflight `capability` exits 2 when ungrounded, and runPreflight throws to
    // abort the command before its prompt is expanded.
    const name = (input.command || "").replace(/^\//, "")
    if (specGovernedCapabilities.has(name)) {
      runPreflight("capability", [name, baseDir(ctx), input.sessionID || "opencode-plugin"])
    }
  },
  "shell.env": async (input, output) => {
    // Sessionless compiles/binds are a real runtime state (undocumented
    // sessionID, only typed optional upstream), not a theoretical one — never
    // throw here, and set nothing when input.sessionID is absent.
    const sid = input && input.sessionID
    if (!sid || !output) return
    if (!output.env) output.env = {}
    output.env.OPENCODE_SESSION_ID = sid
  },
  "tool.execute.before": async (input, output) => {
    const files = targetFiles(ctx, input.tool || {}, output.args || {})
    for (const file of files) {
      const sid = input.sessionID || "opencode-plugin"
      const turn = turnBySession.get(sid) || ""
      runPreflight("write", [file, sid, turn])
    }
    // Bash/shell blind spot (A2/A3): targetFiles() yields [] for the bash
    // tool, so a recognized-but-unclassified mutation path would otherwise
    // reach no guard. Pass every documented bash command, verbatim as one
    // argv element, to exactly the two guards Claude wires on its Bash
    // matchers — no JS command classifier, no `shell` alias.
    const toolName = typeof input.tool === "string" ? input.tool : input.tool?.name || ""
    const command = output.args && output.args.command
    if (toolName === "bash" && typeof command === "string" && command) {
      const cwd = baseDir(ctx)
      const sid = input.sessionID || "opencode-plugin"
      const turn = turnBySession.get(sid) || ""
      runPreflight("worktree-path", ["--tool", "Bash", "--command", command, "--cwd", cwd, "--session", sid])
      const materialArgs = ["check", "--tool", "Bash", "--command", command, "--cwd", cwd, "--session", sid]
      if (turn) materialArgs.push("--turn", turn)
      runPreflight("material-route", materialArgs)
    }
  },
  "tool.execute.after": async (input, output) => {
    const args = input.args || output.args || {}
    const files = targetFiles(ctx, input.tool || {}, args)
    for (const file of files) {
      if (isDesignHtml(file)) runPreflight("design", [file])
    }
    // Read-grounding marker — record actual prd.md and core/*.md reads so the
    // spec gate and core-first adapter guard can pass. Mirrors Claude's
    // PostToolUse[Read] marker pair.
    // Non-blocking: a marker failure must never abort a successful read.
    const toolName = typeof input.tool === "string" ? input.tool : input.tool?.name || ""
    if (toolName === "read") {
      const readFile = normalizeFile(ctx, args.filePath || args.path || args.file)
      if (readFile) collectPreflight("read", [readFile, input.sessionID || "opencode-plugin"])
    }
  },
  })
}
