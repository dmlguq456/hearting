// Human-visible only. Official OpenCode 2 TUI slot; no server/client/model calls.
// Outside the session shell this is the Hearting part of Claude's status line:
// the answer wait, the open route and a waiting session-tidy result, read from
// the same helper and cache. Inside the shell its panel shows them; nothing here.
import { Show, createEffect, createSignal } from "solid-js"
import { execFile } from "node:child_process"
import { fileURLToPath } from "node:url"
import path from "node:path"

export function setupRouteStatus(context) {
  if (process.env.HEARTING_SHELL_SOCKET) return
  if (!context?.ui?.slot || !context?.ui?.router?.current) return
  const [status, setStatus] = createSignal({ sid: "", wait: "", label: "", notice: "" })
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
    if (!sid) { setStatus({ sid: "", wait: "", label: "", notice: "" }); return }
    // The helper reads only the existing private cache and registers display
    // demand. All registry/process/route work belongs to its detached producer.
    pending = execFile("python3", [helper, "--statusline", "opencode", sid, "--with-tidy-notice"],
      { timeout: 2000, maxBuffer: 4096, windowsHide: true }, (error, stdout) => {
        pending = null
        if (disposed) return
        const lines = error ? [] : stdout.split("\n")
        const safe = (text, limit) => typeof text === "string" && text.length <= limit
          && !/[\x00-\x1f\x7f]/.test(text) ? text : ""
        // `<all sessions' waits> <this session's>`: the badge appears while this
        // session waits and counts every wait, as Claude's `◑답 대기 N` does.
        const counts = (lines[0] || "").split(" ")
        const wait = counts.length === 2 && /^[1-9]\d*$/.test(counts[1])
          && /^(\?|\d+)$/.test(counts[0]) ? counts[0] : ""
        setStatus({ sid, wait, label: safe(lines[1], 256),
          notice: safe(lines[2], 300) })
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
  const current = () => status().sid === selected() ? status() : null
  const unregister = context.ui.slot({ append: "prompt.footer.status", render: () => (
    <box flexDirection="row" gap={1}>
      <Show when={current()?.wait}>
        <text fg={context.theme.error ?? context.theme.text.base}>{`◑답 대기 ${current()?.wait}`}</text>
      </Show>
      <Show when={current()?.label}>
        <text fg={context.theme.warning ?? context.theme.text.base}>{current()?.label}</text>
      </Show>
      <Show when={current()?.notice}>
        <text fg={context.theme.warning ?? context.theme.text.base}>{`⚠ ${current()?.notice}`}</text>
      </Show>
    </box>
  ) })
  return () => {
    disposed = true
    clearInterval(timer)
    pending?.kill()
    unregister?.()
  }
}
