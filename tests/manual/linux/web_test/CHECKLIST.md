# Manual Web UI Test Checklist — Phase 8.1–8.7

Run `bash tests/manual/linux/web_test/setup.sh` first.
Keep a second terminal open for `csl-agent approvals` commands.

**Important:** if you restart the agent between test steps, it automatically reattaches to
running persistent jobs via `~/.csl/agent/running.json` — no manual intervention needed.

---

## Prerequisites

- [ ] Setup script completed successfully (`State file: /tmp/csl_web_test.env` printed)
- [ ] Control station running on http://127.0.0.1:16534
- [ ] Agent running (`curl http://127.0.0.1:36717/healthz` returns JSON)

---

## 8.2 — Login page

- [ ] Open http://127.0.0.1:16534 — redirects to `/login`
- [ ] Login page renders without errors; no nav bar shown
- [ ] Submit with wrong password → error message shown inline, stays on `/login`
- [ ] Submit with correct credentials → redirects to `/`

---

## 8.3 — Dashboard

- [ ] Dashboard shows the **localhost-test** machine card
- [ ] Ping badge loads via HTMX within ~5 s (shows "Online · Xms" in green)
- [ ] Machine card shows `127.0.0.1:22` and `linux`
- [ ] Admin nav links visible: Scripts / Machines / Users / Audit
- [ ] Clicking "Details" navigates to `/machines/{id}`

---

## 8.4 — Machine detail page

- [ ] Page heading shows **localhost-test**
- [ ] SSH meta info and platform visible under the heading
- [ ] Ping badge reloads on page load (HTMX `hx-trigger="load"`)
- [ ] **Scripts table** shows three rows: `counter`, `hello`, `sysinfo`
- [ ] All three scripts show state **absent** (grey badge) with a ↻ refresh button
- [ ] Running Jobs section is absent (no jobs yet)

---

## 8.5 + 8.7 — Approval flow: hello (one-off, string param)

### First run — triggers staging

- [ ] Click **Run** on `hello` → navigates to run form
- [ ] Run form shows one field: `greeting` (text, required, marked with `*`)
- [ ] Submit with empty greeting → browser blocks (required field)
- [ ] Fill in greeting (e.g. "World"), click **Run**
- [ ] Response shows approval-error: *"Script 'hello' is pending approval on this machine"*
- [ ] Status is 409, form stays visible with the error

### Approve via CLI

```bash
uv run csl-agent approvals list        # hello: PENDING
uv run csl-agent approvals approve hello
```

- [ ] `approve hello` succeeds without error
- [ ] Back on the machine detail page, click **↻** next to `hello`
- [ ] Badge updates in place (no page reload) to **approved** (green)

### Run after approval

- [ ] Fill greeting again, click **Run** → redirects to `/jobs/{uuid}`

---

## 8.6 — Job detail: hello (non-persistent)

- [ ] Job detail page loads with UUID in heading
- [ ] Script name shows **hello**, machine shows **localhost-test**
- [ ] Status shows **completed**, exit code **0**
- [ ] Log viewer (dark background) shows the script output:
  ```
  Hello, World!
  Host: <hostname>
  ...
  ```
- [ ] SSE status indicator goes: `connecting…` → `streaming` → `done`
- [ ] No **Kill job** button (non-persistent job)

---

## 8.7 — Approval badge: state refresh

- [ ] Go back to `/machines/{id}`
- [ ] `hello` badge shows **approved** (green) without needing a page reload
  *(if not: click ↻ to pull current state from agent)*
- [ ] Hover over badge → tooltip shows approved MD5 hash
- [ ] `counter` and `sysinfo` still show **absent**; click ↻ to confirm

---

## 8.5 + 8.6 — Persistent job: counter (live log + kill)

### Stage and approve

- [ ] Click **Run** on `counter` → approval error (pending, 409)

```bash
uv run csl-agent approvals approve counter
```

- [ ] Click **↻** next to `counter` on machine detail → badge updates to **approved**

### Run and stream

- [ ] Click **Run** on `counter` → run form (no params)
- [ ] Click **Run** → redirects to job detail
- [ ] Log viewer streams `Count: 0`, `Count: 1`, … in real time (auto-scrolling)
- [ ] SSE status shows **streaming**
- [ ] Status badge shows **running**
- [ ] Machine detail page shows `counter` in **Running Jobs** section

### Kill

- [ ] On job detail, click **Kill job** → confirmation dialog appears
- [ ] Confirm → Kill button replaced with **Killed** badge (HTMX inline swap)
- [ ] Counting stops; SSE status shows **done**
- [ ] Machine detail page (refresh) no longer shows counter in Running Jobs

---

## 8.5 — Form types: sysinfo (int param + required string)

### Stage and approve

- [ ] Click **Run** on `sysinfo` → approval error (pending)
- [ ] Machine detail: `sysinfo` shows **pending** badge + **Re-stage** button

```bash
uv run csl-agent approvals approve sysinfo
```

- [ ] Click **↻** next to `sysinfo` → badge updates to **approved** in place

### Run with params

- [ ] Click **Run** on `sysinfo`
- [ ] Run form shows two fields:
  - `message` — text, required (`*`)
  - `repeat` — number, default=3, min=1, max=10
- [ ] Try repeat=15 → browser `max` validation blocks submission
- [ ] Try repeat=0 → browser `min` validation blocks submission
- [ ] Fill message="CSL test", repeat=5, click **Run**
- [ ] Job detail log shows system info then "CSL test" repeated 5 times
- [ ] Exit code **0**, status **completed**

---

## 8.2 — Logout

- [ ] Click **Log out** → redirects to `/login`, flash "Logged out" shown
- [ ] Navigate to `/` → redirects back to `/login` (cookie cleared)

---

## Teardown

```bash
bash tests/manual/linux/web_test/teardown.sh
```

- [ ] Purges orphaned approval entries automatically
- [ ] Machine deleted from control station DB
- [ ] Test scripts deleted from control station DB
- [ ] Agent process stopped
