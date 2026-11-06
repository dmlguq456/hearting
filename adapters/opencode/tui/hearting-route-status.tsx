// Human-visible only. Official OpenCode 2 TUI slot; no server/client/model calls.
import { createEffect, createSignal } from "solid-js"
import { execFile } from "node:child_process"
import { fileURLToPath } from "node:url"
import path from "node:path"

export function setupRouteStatus(context) {
  if (!context?.ui?.slot || !context?.ui?.router?.current) return
  const [status, setStatus] = createSignal({ sid: "", label: "" })
  const root = process.env.AGENT_HOME || path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..")
  const helper = path.join(root, "utilities/human_wait_state.py")
  let disposed = false, pending = null, requested = ""
  function selected() {
    const route = context.ui.router.current()
    return route?.type === "session" && typeof route.sessionID === "string"
      ? route.sessionID : ""
  }
  function refresh() {
    const sid = selected()
    if (disposed || pending) return
    if (!sid) { setStatus({ sid: "", label: "" }); return }
    // The helper reads only the existing private cache and registers display
    // demand. All registry/process/route work belongs to its detached producer.
    pending = execFile("python3", [helper, "--statusline", "opencode", sid],
      { timeout: 2000, maxBuffer: 4096, windowsHide: true }, (error, stdout) => {
        pending = null
        if (disposed) return
        const label = error ? "" : (stdout.split("\n")[1] || "")
        const safe = label.length <= 256 && !/[\x00-\x1f\x7f]/.test(label) ? label : ""
        setStatus({ sid, label: safe })
        if (selected() !== sid) refresh()
      })
  }
  createEffect(() => {
    const sid = selected()
    if (requested !== sid) { requested = sid; refresh() }
  })
  const timer = setInterval(refresh, 5000)
  timer.unref?.()
  refresh()
  const unregister = context.ui.slot({ append: "prompt.footer.status", render: () => (
    <text fg={context.theme.text.base}>{status().sid === selected() ? status().label : ""}</text>
  ) })
  return () => {
    disposed = true
    clearInterval(timer)
    pending?.kill()
    unregister?.()
  }
}
