```
hearting run capability-route compose --start --prompt-file <task> \
  [--shape direct] [--campaign-key <stream>]
```

Shape-free flags are hints. Follow decided routes under §0.2.1; person-named
stages use `--shape staged --graph <stage,…>`.

`hearting` is on PATH. `hearting run <utility>` uses `AGENT_HOME` when set (dev
activation: that checkout), else the installed release. The slug comes from the
task. The stream defaults to this session's latest artifact-root stream, else
this folder's only active one; otherwise use `--campaign-key` (a folder name or
close spelling joins that stream; refusal lists keys) or `--unassigned`. The
runtime prepares the cycle, selects each leg's harness from live usage within
sealed candidates, starts frames and owner, reuses exact attempts, and closes
the route and cycle at success or idle.

Follow the receipt. `parent_next=end-turn`: end with no wait, poll or recap;
a runtime carrier owns it. `parent_next=bounded-wait`: run `parent_next_command`
once. Start saves `receipt_file`; read it if hooks omit `parent_next`.
No directive is not `end-turn`; never filter stdout.
After wake/correction, run `resume_command`; answer a BLOCKED owner with `correction_command
--message-file <file>`. At `needs-question`, compare the two frame briefs and
follow the receipt's `next_step`: `resume_command --interview <file>` registers
the question, the person answers it once in the native question surface, and
`resume_command --answers <file>` records the intent, releases the gate and
starts the owner.

Inside an owner, a stage is `python3 "$AGENT_HOME/utilities/stage-dispatch-fallback.py"
--node <node> --start`: route, slug, parent and harness come from the owner's
route and environment, and a member of a sealed parallel group starts its
whole group in one batch. Dispatch depth 3 is forbidden.

Default to this session's route; start an interactive peer with `hearting run
peer-steward start` only for long work needing human answers or recovery.
Command output panes use `hearting run pane open <command>`.

Message another session only with `hearting run peer-steward prompt
<name-or-pane> --body-file <file>`; reply text addressed to it reaches nobody.
