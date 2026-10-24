# Claude design runtime details

These commands apply only when the Claude adapter owns a design run. Other
adapters use their verified visual-harness or browser-render contract and the
portable static-render fallback.

## Design MCP setup and verification

```sh
test -d "$HOME/.claude/tools/design-mcp/node_modules" && echo 'design-mcp:installed' || echo 'design-mcp:needs-install'
claude mcp list 2>/dev/null | grep -q '^design:' && echo 'design-mcp:registered' || echo 'design-mcp:unregistered'
(cd ~/.claude/tools/design-mcp && npm install)
claude mcp add design --scope user -- node ~/.claude/tools/design-mcp/server.js
(cd ~/.claude/tools/design-mcp && npm run smoke)
claude mcp list 2>/dev/null | grep -i figma
```

The MCP may attach only in a new session after registration. Use the selected
static renderer for the current cycle when needed. The `~/.claude/tools/design-mcp/`
converter exposes PDF, self-contained HTML bundle, and PPTX output. Its post-write
hook is at `~/.claude/hooks/design-postwrite.sh` and can be disabled with
`DESIGN_POSTWRITE_HOOK=0`.
