```
hearting run capability-route compose --start --prompt-file <task> \
  [--shape direct] [--campaign-key <stream>]
```

Use `--shape direct` only for one atomic, reversible inline change; otherwise
omit it and let the frame decide. Other shape-free flags are hints. Follow
decided routes under §0.2.1; person-named stages use
`--shape staged --graph <stage,…>`.

`hearting` is on PATH. `hearting run <utility>` runs that harness utility from
`AGENT_HOME` when set (a dev checkout), else from the installed release. The
task names the slug. The stream defaults to this session's latest in the
artifact root, else this folder's only active one; otherwise name it with
`--campaign-key` (a folder name or close spelling joins it; a refusal lists
the keys) or opt out with `--unassigned`. Runtime prepares the cycle, picks
each leg's harness by live usage among sealed candidates, starts frames and
owner, reuses exact attempts, and closes route and cycle at success or idle.

Follow the receipt. `parent_next=end-turn`: end with no wait, poll or recap;
a runtime carrier owns it. `parent_next=bounded-wait`: run `parent_next_command`
once. Start saves `receipt_file`; read it if hooks omit `parent_next`.
No directive is not `end-turn`; never filter stdout.
After wake/correction, run `resume_command`; answer a BLOCKED owner with `correction_command
--message-file <file>`. At `needs-question`, compare the two frame briefs and
follow the receipt's `next_step`: `resume_command --interview <file>` registers
the question, the person answers once in the native question surface, and
`resume_command --answers <file>` records the intent, releases the gate and
starts the owner.

Inside an owner, a stage is `python3 "$AGENT_HOME/utilities/stage-dispatch-fallback.py"
--node <node> --start`: route, slug, parent and harness come from the owner's
route and environment, and a sealed parallel group's member starts its
whole group in one batch. Dispatch depth 3 is forbidden.

Message another session only with `hearting run peer-steward prompt
<name-or-pane> --body-file <file>` (reply text addressed to it reaches nobody);
start one with `peer-steward start` and a command output pane with
`hearting run pane open <command>`.
