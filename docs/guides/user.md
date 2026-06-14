# User guide

For people who use the control-station-lite web UI to run scripts on machines.

This guide assumes someone has already set up the control station and given you a
login. If you administer the control station itself, see the
[admin guide](admin.md) and [operator guide](operator.md). If you own a machine
that runs the agent, see the [target-owner guide](target-owner.md).

---

## Logging in

Open the control station in your browser (your operator will give you the URL —
typically `https://<nas-hostname>/`) and sign in with the username and password
you were given. Sessions are kept alive with a short-lived access token and a
longer-lived refresh cookie, so you stay logged in across page reloads but are
signed out after a period of inactivity.

---

## The dashboard

The dashboard lists the machines you have **bookmarked**. Each row shows the
machine name and a live reachability badge that polls in the background:

- **Reachable** — the machine answers on SSH right now. This is a pure network
  check; it does *not* start or talk to the agent, so it stays cheap.
- **Unreachable** — no SSH response. The machine may be powered off (try
  Wake-on-LAN from the machine detail page) or off the network.

Reachability says nothing about whether the agent is running — the agent only
starts when you actually run something.

---

## Opening a machine

Click a machine to open its detail page. You get three things:

1. **Status panel** — reachability and, if the agent happens to be up, its
   health and any persistent jobs currently running.
2. **Running jobs** — persistent jobs in progress, each linking to its live log.
3. **Available scripts** — every script in the library, each with an
   approval-state badge (see below).

When the page opens it does a single batched refresh: it opens one SSH tunnel to
the machine, makes sure the agent is running, and updates every script badge at
once. That one action is why the first load of a machine page can take a second
or two.

---

## Reading script-state badges

Every script shows a badge describing its state **on that specific machine**.
The same script can be approved on one machine and rejected on another — state
is per machine, and it is decided by the machine's owner, not by you or the
admin.

| Badge | Meaning | Can you run it? |
| --- | --- | --- |
| **approved** | The owner approved this exact version. | Yes. |
| **pending** | Staged on the target, waiting for the owner's first approval. | No — ask the owner to approve. |
| **update_pending** | The owner approved an *older* version; the current one needs re-approval. | No — the old version is blocked too, until re-approval. |
| **rejected** | The owner explicitly refused this script. | No — and it will not be retried. |
| **approved_stale** | Approved version no longer matches the library's canonical version; it needs re-staging. | No — use **Re-stage for approval**. |
| **absent** | Not yet on the target. | No — running it will stage it first. |

Hover a badge to see the approved/pending MD5 fingerprints. For `pending`,
`update_pending`, and `approved_stale`, a **Re-stage for approval** button
nudges the target to re-check and re-prompts the owner.

Why all this ceremony? Approval is bound to specific script *content*. If an
admin edits a script, the previously granted approval no longer applies — the
owner has to look at the change and approve it again. This is the whole point of
the project: the machine's owner stays in control of what runs.

---

## Running a script

1. On the machine detail page, click **Run** next to an `approved` script.
2. A dialog renders a form built from the script's metadata. Fields appear per
   declared parameter — text boxes, numbers, checkboxes, dropdowns, or path
   inputs — with the help text the author wrote. Required fields are enforced
   before you can submit.
3. Submit.
   - **One-off script:** runs to completion; its output is captured and viewable.
   - **Persistent script** (e.g. a server process): you're taken to the job's
     live log view, where it keeps running until killed.

### When a run is blocked

If approval state changed between page load and submit, you'll get a clear
message instead of a generic error — `pending_approval (new)`,
`pending_approval (update)`, or `rejected`, with the current state. The fix is
always the same: the machine's owner approves the script via their agent CLI
(see the [target-owner guide](target-owner.md)). The control station never
bypasses approval.

---

## Live logs

Persistent jobs (and any job's captured output) stream into a live log viewer:

- Output appends in real time over a server-sent event stream and auto-scrolls.
- Reconnecting replays the recent tail so you're not left staring at a blank
  pane.
- A **Kill** button stops a running persistent job. The agent terminates the
  process and its children.

---

## Wake-on-LAN

If a machine has a MAC address on record, its detail page has a **Wake-on-LAN**
button that broadcasts a Magic Packet to wake it. Use it when the reachability
badge shows the machine is off. Waking is a built-in action — it needs no script
and no approval.

---

## Job history

The **Jobs** link in the navigation bar opens a filterable history of every run
(by machine, script, or status). Each row links to that job's detail and log
view, so you can revisit the output of a past run. You only see jobs for
machines you have access to.

---

## Quick troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| Machine shows **Unreachable** | Powered off or off-network | Try Wake-on-LAN; check it's on your network |
| Script badge stuck on **pending** | Owner hasn't approved yet | Ask the owner to run `csl-agent approvals approve <name>` |
| Run rejected with **rejected** state | Owner refused the script | Nothing to retry — talk to the owner |
| **update_pending** after an edit | Script content changed | Owner must re-approve the new version |
| First machine-page load is slow | One-time tunnel + agent start | Normal; subsequent badge refreshes are faster |
