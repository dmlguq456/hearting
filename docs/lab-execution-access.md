# Lab Run Storage and Explicit Data Access

When an owner starts or resumes a route whose `part_catalog` row declares an
origin stage that needs compute execution, the exact `run_root` of the
initialized compute-hosts inventory is added to the existing
`execution_access_v1` request. The requirement is declared in one place, on the
origin stage's row in the `part_catalog` of `capabilities/topologies.json`, and
is looked up by the sealed node's stage identity. A borrowed stage uses the same
declaration. The inventory is read with the existing loader from
`COMPUTE_HOSTS_CONFIG`, or from
`${XDG_CONFIG_HOME:-~/.config}/hearting/compute-hosts.yaml`. This default keeps
the log and PID storage of GPU work and compute-hosts resource runs inside the
owner's normal access range. It is not added to ordinary code owners or frames
that have no compute stage. A resource child only gets access inside its actual
parent's grant.

A lab owner with a GPU execution scope receives the inventory folder read-only.
Unless a separate explicit request says otherwise, the compute-hosts SSH network
need is added to the same request. The OpenCode worker home keeps the exact path
of the user's inventory in the existing `COMPUTE_HOSTS_CONFIG` input before the
XDG paths are isolated. No file is copied and no user setting is changed. A route
that already has a prepared file also gets this execution default prepared on a
normal start or correction, keeping the roots it already derived. Non-execution
children get no such automatic request, and a child's actual request cannot
exceed its parent's grant. For Claude and OpenCode, `network_allowed` is a
logical permission, not a guarantee of OS network isolation.

If the inventory is missing or still the commented template, no path is guessed.
A malformed inventory, a root as broad as the whole home folder, and a request
that reaches a broad root through a symlink are all refused by the existing
validation. Preparation only reads the inventory and writes the request file; it
does not create the actual `run_root` or start any work.

## Naming external data paths

When there is no manual request file, start and resume build the request from the
sealed task text (`execution_access.derive_task_access`).

- Read roots: every existing folder among the absolute paths in the task text
  (for a file, its folder). An `artifact_root_path` or absolute path string in
  the `roots` of a JSON list that the task text names is also a read input. The
  installed app's `ops/` input list and the Hearting code and documents can be
  read as well. Reads are capped at 256 roots and writes at the existing 16. The
  input list is read safely up to 64 KiB, and the derived request and binding up
  to 4 MiB. A link in a source folder that escapes the installed source area is
  not granted automatically. A development checkout that already has write
  approval does not get a read-only restriction layered on top.
- Write roots: only the existing folders in a clause of the start card's
  `범위:` (`Scope:`) field that contains a write expression (write, save, modify,
  create, 쓰기, 저장, 수정, 생성 and similar) and no read or exclusion
  expression, and only for the owner. A path-less clause after a path, in
  parentheses or after a comma (`(excluded)`, `(read-only)`, `, read only`),
  attaches to the path just before it. A path with no write expression, or one
  that mixes read and write, is a read root. From the first path that carries an
  exclusion expression (excluded, forbidden, not, off-limits and similar) onward,
  paths are left out of reading as well. A doubtful reading falls toward less
  access. A path that appears elsewhere in the task text never becomes a write
  root.
- Left out: credentials, keys, hidden folders in the home such as `~/.ssh`, user
  and runtime settings, system areas, paths that do not exist, paths that are too
  broad, and places that are already writable such as the worktree and the
  artifact root.
- Evidence: each root records its source in the request's `justification` (the
  scope field or task text, which line, the expression that decided it, and that
  clause). The `derivation` in the prepared `binding.json` records the roots
  received and the paths left out, with the reason.
- A node of the same route reuses its first prepared result on resume. Read
  inputs that an earlier installed release missed are added once on a normal
  start: past requests and their read/write decisions are preserved first, only
  the remaining read capacity is filled, and the supplemented list is reused
  afterwards. If a derived root fails validation, only those roots are dropped and
  the start proceeds; derivation never causes a start to be refused.

A manual request file is still accepted, and when present it takes precedence over
derivation. The path below is an example and must be replaced with the actual
approved path.

```json
{
  "schema_version": 1,
  "writable_roots": ["/data/project/train-valid"],
  "read_roots": [],
  "network": {"required": false, "reason": "", "hosts": []},
  "enforcement_required": "os-sandbox",
  "justification": {
    "/data/project/train-valid": "User-approved data processing and result storage path"
  }
}
```

After saving the file, pass it to the session that runs the existing start or
resume command.

```sh
export AGENT_DISPATCH_EXECUTION_ACCESS_FILE=/absolute/path/lab-access.json
# run the existing preflight.sh compose --start ... or the receipt's resume_command
```

Lab preparation validates this file with `load_request`, adds only the inventory's
`run_root`, and stores the copy in the route's `execution-access` folder under the
existing dispatch state. The original file is not modified. Keeping the same input
across start and resume reuses the same prepared file.

To change the request on an open route, the parent supplies a new file through the
same environment variable and runs that route's `start` (or `correct`). Only when
it is the route's current parent, one `access` row (with who supplied it and the
source; a first derived request is a `derived` row) is added to the route's
change record beside the route. The next owner, a replacement owner and their
children then start with the request in the latest row. A lab owner uses a
separate prepared file made from that request plus `run_root`. Another session's
request is not recorded. An explicit JSON takes precedence over the preview ROOTS
input. Low-level direct dispatch can still use the existing
`--execution-access-file <JSON>`; on that direct path, every needed root must be
named explicitly in that JSON.

Owner and child handoff use the existing environment variable, CLI and
effective-grant paths. A child request that exceeds the parent's actual file or
network scope is refused before any model starts. No separate approval step or
global permission setting is needed.

## Enforcement levels and current limits

### Execution sandbox for GPU compute parts

When a catalog part declares `execution_needs: compute-hosts`, the Codex owner and
that compute part use the existing `danger-full-access`. This covers not only
the lab's smoke, full-run and run-verify but also eval/resume and the compute
parts, extra stages and parallel legs that a code route borrows. The catalog
declaration is the basis for the compute decision. An existing explicit
`resource_class=gpu` and the resource-runner's `gpu` promotion signal are kept as
compatible selectors, and the natural language of a task description is not
interpreted. Ordinary code, frames, and non-compute children such as planning,
metrics and report keep the default.

A new route makes one selection that is shared by prospective readiness, the
sealed `codex_execution_sandbox`, the actual exec/App Server run, and GPU lease
admission. A route that is already sealed keeps the GPU scope and sandbox of its
seal. A historical route with no seal keeps its existing owner sandbox selection;
the checked Codex parent tuple alone does not change the sandbox or retroactively
create compute access or a GPU lease. A lab or code suffix owner of the same
cycle inherits the seal of the completed prefix, and the non-compute children
below it keep the default. The caller's existing `--sandbox`,
`CODEX_DISPATCH_SANDBOX` and `CODEX_DISPATCH_SANDBOX_FORCE` precedence is
preserved, and the user's global settings are not modified.

On the Linux sandbox of Codex 0.160.0, NVIDIA devices were confirmed by direct
query to be hidden. Reading `/dev` did not fix it, and a device write profile
ended in a bwrap panic. In full access on the same installation, the
`nvidia-smi -L` query succeeded. This does not guarantee that training works or
that any other external sandbox supports the devices.

This selection has **no** filesystem or network OS enforcement. In this scope,
only requests with `enforcement_required=any` are accepted, as a logical
root/child-at-most-parent boundary, and recorded with the grade `none`,
`file-enforcement-none` and `network-enforcement-none`. A network request stays
`granted-unenforced` within the parent's logical allowance. A request that
requires `os-sandbox`, a write request under read-only, and a request outside the
parent are refused as before. If strict OS enforcement is needed, use the
existing workspace execution or the compute-hosts resource path. Compose, explain,
start and the launch receipt show the actual selection and the level applied, and
external sandboxes and managed runtime constraints continue to apply.

### Host payload parts without a GPU

Long measurements, benchmarks, browser tests and memory-capped commands that need
no GPU borrow `autopilot-lab:host-run`
(`execute,autopilot-lab:host-run,test,report`) and run as a registered, detached,
supervised resource. The catalog declaration is `execution_needs: host-process`;
it is not a compute part, so it does not require GPU scope, a lease, access to
compute-hosts storage, a smoke attestation or a start approval.

The Codex owner and model children stay `workspace-write`. Only when this node is
confirmed as a host part in a sealed route does the payload command made by the
controller run outside the owner profile (`-P :danger-full-access`), so that
`systemd-run --user --scope -p MemoryMax=… -p MemorySwapMax=0 <command>` inside
the payload can reach the user systemd manager. The memory cap is owned by that
payload command; Hearting adds no separate option or proof file. This payload has
**no** filesystem or network OS enforcement. If the caller supplied `--sandbox`,
`CODEX_DISPATCH_SANDBOX` or `CODEX_DISPATCH_SANDBOX_FORCE`, that choice applies
unchanged, and a request that already carries `enforcement_required=os-sandbox` is
refused before the payload starts with
`execution-access-enforcement-unavailable`. With `any`, or with no request, no new
input is required. Earlier routes that were already sealed are not changed
retroactively. Claude and OpenCode use the same part, and no sandbox branch is
added.

The official documentation also explains that full access removes the file and
network boundaries:
[Codex sandbox](https://learn.chatgpt.com/docs/sandboxing),
[Codex permissions](https://learn.chatgpt.com/docs/permissions),
[Codex App Server](https://learn.chatgpt.com/docs/app-server)

Under Codex `workspace-write`, writable roots are applied to the OS sandbox. The
local `codex-cli 0.160.0` exec passes the same validated result with `--add-dir`,
and the Hearting App Server runner with `--writable-root`. The existing default
path of ordinary code runs and the builder argv without a request are unchanged.
The official Codex configuration also supports per-path file permissions and
workspace extension.
[Codex permissions configuration](https://learn.chatgpt.com/docs/config-file/config-reference)

`read_roots` are projected read-only on all three harnesses (the adapter
declaration table, `access`). The Codex sandbox reads everywhere and writes only
to writable roots; Claude uses `--add-dir` and an Edit deny rule for each root;
OpenCode uses read-only `external_directory`. The actual guarantee level is
recorded in the grant's `read_enforcement`.

If a network such as SSH is needed, state the existing `network.required=true` and
a `reason`, and follow the existing owner network policy. `network.hosts` does not
currently guarantee per-host isolation. With `any`, the unenforced state is
reported; requiring both a host restriction and `os-sandbox` is refused. The
Codex documentation likewise says that per-host network rules need proxy
enforcement.
[Codex network configuration](https://learn.chatgpt.com/docs/config-file/config-reference)

On Claude and OpenCode the same request is applied at the existing
tool-permission level and is not reported as equivalent to an OS sandbox. Claude's
additional-directory permissions and its sandbox are separate settings.
[Claude permissions](https://code.claude.com/docs/en/permissions)

## Refreshing checked evidence for a continuation

When the sandbox of an already running parent differs from the original tuple, the
existing continuation command can optionally take
`--dispatch-evidence /absolute/path/checked-evidence.json`. The evidence is
verified with the same exact-worktree and parent-identity checks as compile and
compose, and is reflected only in the new continuation's tuple and its fallback
selection. The original route and completion evidence, the unit, the approval
scope and the cycle lineage are preserved. Without the override, the original
evidence is used unchanged. This input does not change the sandbox and performs
no automatic probe or privilege escalation. A new normal GPU compose/start
prepares the execution selection together with its probe and tuple, so this
recovery input is not needed there.
