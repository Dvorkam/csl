# Admin guide

For people who administer the control station's **content** — the script library,
users, and machines. (For deploying and operating the control station process
itself — backups, TLS, key rotation — see the [operator guide](operator.md).)

Admins hold the canonical copy of every script. But an admin can never force a
script onto a machine: staging an edited script only moves it to `pending` /
`update_pending` on the target, and the machine's owner still has to approve it.
Keep that in mind — your job is to author good scripts, not to push them.

---

## Writing a script

A script is a plain shell script (`.sh` for Linux/macOS, `.ps1` for Windows) plus
an optional metadata file. The agent runs it on the target with parameters passed
as environment variables.

### Parameters: the `CSL_PARAM_` convention

Declared parameters arrive as environment variables, uppercased and prefixed
`CSL_PARAM_`. This avoids all shell-quoting and positional-argument pitfalls and
behaves identically across platforms.

A parameter `model_path` is read as:

```bash
# .sh
echo "Loading $CSL_PARAM_MODEL_PATH"
```

```powershell
# .ps1
Write-Host "Loading $env:CSL_PARAM_MODEL_PATH"
```

A script with **no** metadata file accepts **no** parameters — the agent rejects
any params for it. Declare every parameter you intend to read.

### Persistent vs one-off

Set `persistent: true` in metadata for long-running processes (a server you want
to supervise and stream logs from). Run it in the **foreground** — the agent
supervises the process directly, streams its output, and kills it (and its child
processes) on request. A one-off script (`persistent: false`, the default) is
expected to run to completion.

### A complete example

`backup_photos.sh`:

```bash
# shellcheck shell=bash
set -euo pipefail
dest="${CSL_PARAM_DEST}"
echo "Backing up to ${dest}"
rsync -a "$HOME/Photos/" "${dest}/"
```

`backup_photos.meta.yaml`:

```yaml
description: |
  Back up the Photos folder to a destination path.
persistent: false
params:
  - name: dest
    type: path
    required: true
    help: "Destination directory for the backup."
```

---

## Metadata reference (`<name>.meta.yaml`)

A metadata file is required only when a script has parameters or you want a
description/tags shown in the UI. It must share the script's base name
(`backup_photos.sh` → `backup_photos.meta.yaml`).

```yaml
description: |
  Multi-line description shown in the run dialog. Markdown supported.

persistent: false           # default false; true = long-running job

tags:                       # optional, for UI grouping
  - llm
  - dev-tools

params:
  - name: model_path
    type: string            # string | int | float | bool | choice | path
    required: true
    help: "Filesystem path to the GGUF model file."

  - name: context_size
    type: int
    default: 4096
    min: 512
    max: 32768
    help: "Context window size."

  - name: gpu_layers
    type: choice
    choices: [0, 16, 32, "all"]
    default: "all"
    help: "Number of layers to offload to GPU."
```

### Field reference

| Field | Applies to | Notes |
| --- | --- | --- |
| `description` | script | Shown in the run dialog. Markdown supported. |
| `persistent` | script | `true` for supervised long-running jobs. Default `false`. |
| `tags` | script | Optional list for UI grouping. |
| `params[].name` | param | Becomes `CSL_PARAM_<NAME>`. |
| `params[].type` | param | One of `string`, `int`, `float`, `bool`, `choice`, `path`. |
| `params[].required` | param | If true, the form won't submit without it. |
| `params[].default` | param | Pre-filled value; used when the field is left blank. |
| `params[].help` | param | Help text shown under the field. |
| `params[].min` / `max` | `int`, `float` | Range bounds, enforced. |
| `params[].choices` | `choice` | Allowed values, rendered as a dropdown. |

Unknown top-level fields are logged and stripped rather than crashing the load, so
a typo won't take the library down — but it also won't do what you intended.
Genuine errors (bad types, missing required fields) are rejected at write time.

### Validation happens twice

The control station validates metadata when you save a script. The **agent** also
re-validates submitted parameters against the *approved* script's metadata before
running anything — unknown params, missing required params, and type / min / max /
choice violations are all rejected at the trust boundary. So the form is a
convenience; the agent is the enforcer.

---

## Managing the script library (UI)

The admin **Script library** page lists every script. From there you can:

- **Create** a new script (name, content, metadata).
- **Edit** an existing one. Editing recomputes the script's MD5. Any machine that
  had the old version approved drops to `update_pending` (or `approved_stale`)
  and is blocked until its owner re-approves — this is the intended consequence
  of changing trusted content.
- **Delete** a script.

The editor is a simple code textarea — enough to author and tweak scripts, not a
full IDE.

---

## Built-in script catalogue

The package ships a starter catalogue. `scripts/setup.sh` seeds it by running
`csl-admin seed-scripts` after creating the first admin, and you can re-run it any
time:

```bash
csl-admin seed-scripts
```

Seeding is **create-if-absent**: it never modifies a script row that already
exists, so your edits are never clobbered. Each platform variant is seeded as its
own row (the name carries the extension); cross-platform scripts share one
metadata file.

| Script | Platforms | Notes |
| --- | --- | --- |
| `sleep_machine` | `.sh` (Linux/macOS), `.ps1` (Windows) | Suspend the machine. |
| `restart_machine` | `.sh`, `.ps1` | Reboot the machine. |
| `start_steam` | `.ps1` (Windows) | Launch Steam (registry-resolved path, `steam://` fallback). |
| `start_llama_server` | `.ps1` (Windows) | Persistent; params `model_path`, `context_size`, `gpu_layers`. Foreground `llama-server` so the agent supervises it. |

Wake-on-LAN is **not** a script — it's a built-in action (no approval needed); see
the [user guide](user.md#wake-on-lan).

Every built-in still has to be approved per machine by its owner before it can
run. Seeding only populates the library.

---

## Managing users and machines

The admin panel also covers:

- **Users** — list, enable/disable, change role (user/admin). You can't disable or
  demote your own account (a guard rail against locking yourself out). Create the
  first admin with `csl-admin create-admin`.
- **Machines** — list, register, and remove machines. Click **Register machine**
  and paste the registration bundle a target owner gave you (from `csl-agent
  init` — see the [target-owner guide](target-owner.md)), along with the SSH host
  and an optional name/user/MAC. The control station connects once over SSH to
  verify the bundle, pins the machine's SSH host key, and then shows its
  **fingerprint** — confirm that with the target owner out-of-band before
  trusting the connection. (The same operation is available as `POST
  /api/machines` for scripted onboarding.)
- **Audit log** — every state-changing action (logins, machine add/remove, script
  create/edit/delete, job submit/kill, Wake-on-LAN) is recorded with who, what,
  target, result, and structured details. Filter by action, target type, or
  username.

---

## Troubleshooting

| Symptom | Cause | Resolution |
| --- | --- | --- |
| Run returns `pending_approval (new)` | Script staged but never approved on the target | Owner runs `csl-agent approvals approve <name>` |
| Run returns `pending_approval (update)` | Script was edited; old approval no longer valid | Owner re-approves the new version (`approvals diff` then `approve`) |
| Run returns `rejected` | Owner refused the script | Nothing to retry; discuss with the owner |
| Badge shows `approved_stale` | Approved MD5 ≠ canonical MD5 | Use **Re-stage for approval** to push the current version |
| Parameter rejected on run | Agent re-validated against approved metadata | Check the param against the script's `.meta.yaml` (type/min/max/choices) |
| `agent-unreachable` error | Agent couldn't be started or tunnel failed | Check machine reachability; confirm the agent service installed (`csl-agent init`) |
| `VERSION_INCOMPATIBLE` at registration | Agent major version ≠ server major version | Upgrade the agent (`pip install -U control-station-lite[agent]`) and re-init |
| Edited a built-in but `seed-scripts` didn't restore it | Seeding is create-if-absent by design | Delete the row first if you want the packaged version back |

Errors are surfaced with stable machine-readable codes (auth, approval,
agent-unreachable, validation) alongside human-readable detail, so the UI can
render the right next step rather than a generic failure.
