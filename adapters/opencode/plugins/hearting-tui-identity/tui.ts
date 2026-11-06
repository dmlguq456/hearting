// OpenCode 2 terminal-client entry for the Hearting TUI identity plugin.
//
// OpenCode 2 loads a terminal plugin from a discovered package directory
// (<config>/plugins/<name>/) through its `tui` entrypoint, and skips a plugin
// that tui.json or cli.json names as a plain file (v2.0.32
// packages/tui/src/plugin/context.tsx). Runtime activation renders this file
// with the activated bundle's absolute path; the relative import serves a
// source checkout.
import identity from "../../tui/hearting-tui-identity.ts"
import { setupRouteStatus } from "../../tui/hearting-route-status.tsx"

export default { ...identity, setup(context) {
  const stopIdentity = identity.setup(context)
  const stopRouteStatus = setupRouteStatus(context)
  return () => { stopRouteStatus?.(); stopIdentity?.() }
} }
