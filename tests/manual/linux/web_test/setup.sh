#!/bin/bash
# setup.sh — Prepares a local CSL environment for manual web UI testing.
#
# What it does:
#   1. Cleans ~/.csl/scripts.pending/ garbage (from earlier fuzz/test runs)
#   2. Starts sshd if not already running (requires sudo)
#   3. Runs `csl-agent init` to create config.yaml and SSH keys
#   4. Starts the agent in the background
#   5. Logs in to the control station (prompts for credentials)
#   6. Registers the local machine
#   7. Creates the three test scripts (hello, counter, sysinfo)
#
# Usage (from repo root):
#   bash tests/manual/linux/web_test/setup.sh
#
# Teardown:
#   bash tests/manual/linux/web_test/teardown.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../../../.." && pwd)"
SCRIPTS_DIR="$REPO_DIR/tests/manual/linux/web_test/scripts"
SERVER_URL="${CSL_SERVER:-http://127.0.0.1:16534}"
STATE_FILE="/tmp/csl_web_test.env"

cyan()  { printf '\033[36m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

bold "=== CSL Web UI Test Setup ==="
echo

# Wipe any stale state
rm -f "$STATE_FILE"

# ── 1. Clean up fuzz garbage ─────────────────────────────────────────────────
cyan ">>> Cleaning ~/.csl/scripts.pending/ ..."
rm -rf ~/.csl/scripts.pending
mkdir -p ~/.csl/scripts.pending
echo "    Done."

# ── 2. Start sshd ────────────────────────────────────────────────────────────
cyan ">>> Checking sshd..."
if systemctl is-active --quiet sshd; then
    echo "    sshd already running."
    echo "SSHD_STARTED=0" >> "$STATE_FILE"
else
    echo "    sshd not running — starting (requires sudo)..."
    sudo systemctl start sshd
    echo "SSHD_STARTED=1" >> "$STATE_FILE"
    echo "    sshd started."
fi

# Verify SSH is actually listening
if ! ss -tlnp | grep -q ':22 '; then
    red "ERROR: sshd not listening on port 22 after start."
    exit 1
fi

# ── 3. Initialize agent ───────────────────────────────────────────────────────
cyan ">>> Initializing csl-agent..."
cd "$REPO_DIR"
# csl-agent init prints the registration bundle as the last line of stdout.
# Write to a file to avoid shell-interpolation corruption.
BUNDLE_FILE="/tmp/csl_bundle.txt"
# The bundle is a long base64 line; grep extracts it regardless of surrounding text.
uv run csl-agent init 2>/dev/null | grep -E '^[A-Za-z0-9+/=]{100,}$' > "$BUNDLE_FILE"
BUNDLE=$(cat "$BUNDLE_FILE")
if [ -z "$BUNDLE" ]; then
    red "ERROR: csl-agent init produced no bundle output."
    exit 1
fi
echo "    Agent initialized. Bundle captured (${#BUNDLE} chars)."
echo "BUNDLE_FILE=$BUNDLE_FILE" >> "$STATE_FILE"

# Verify config.yaml was created
if [ ! -f ~/.csl/config.yaml ]; then
    red "ERROR: ~/.csl/config.yaml was not created by csl-agent init."
    exit 1
fi
echo "    config.yaml present."

# ── 4. Start agent ────────────────────────────────────────────────────────────
cyan ">>> Starting csl-agent..."
# Kill any stale agent first
pkill -f "csl-agent$" 2>/dev/null || true
sleep 0.5

nohup uv run python -m control_station_lite.agent > /tmp/csl_agent.log 2>&1 &
AGENT_PID=$!
echo "AGENT_PID=$AGENT_PID" >> "$STATE_FILE"
echo "    Agent PID: $AGENT_PID — waiting for healthz..."

for i in $(seq 1 10); do
    sleep 1
    if curl -sf http://127.0.0.1:36717/healthz > /dev/null 2>&1; then
        break
    fi
    if [ "$i" -eq 10 ]; then
        red "ERROR: Agent did not become healthy after 10s."
        red "       Check: tail /tmp/csl_agent.log"
        exit 1
    fi
done
green "    Agent healthy."

# ── 5. Verify control station ─────────────────────────────────────────────────
cyan ">>> Checking control station at $SERVER_URL ..."
if ! curl -sf "$SERVER_URL/healthz" > /dev/null; then
    red "ERROR: Control station not responding. Start it first:"
    red "       uv run csl-server --reload --port 16534"
    exit 1
fi
echo "    Control station is up."

# ── 6. Login (Python handles the JSON) ───────────────────────────────────────
cyan ">>> Login credentials for the control station:"
read -rp "    Admin username [admin]: " ADMIN_USER
ADMIN_USER="${ADMIN_USER:-admin}"
read -rsp "    Admin password: " ADMIN_PASS
echo

echo "ADMIN_USER=$ADMIN_USER" >> "$STATE_FILE"

TOKEN=$(python3 - "$SERVER_URL" "$ADMIN_USER" "$ADMIN_PASS" <<'PYEOF'
import urllib.request, json, sys
server, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(
    f"{server}/api/auth/login",
    data=json.dumps({"username": user, "password": pw}).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    resp = urllib.request.urlopen(req)
    print(json.load(resp)["access_token"])
except urllib.error.HTTPError as e:
    sys.stderr.write(f"Login failed: {e.code} {e.read().decode()}\n")
    sys.exit(1)
PYEOF
)
echo "TOKEN=$TOKEN" >> "$STATE_FILE"
green "    Logged in as $ADMIN_USER."

# ── 7. Register local machine ─────────────────────────────────────────────────
cyan ">>> Registering localhost as a machine..."
MACHINE_ID=$(python3 - "$BUNDLE_FILE" "$SERVER_URL" "$TOKEN" <<'PYEOF'
import urllib.request, json, sys

bundle_file, server, token = sys.argv[1], sys.argv[2], sys.argv[3]
bundle = open(bundle_file).read().strip()

req = urllib.request.Request(
    f"{server}/api/machines",
    data=json.dumps({
        "bundle": bundle,
        "name": "localhost-test",
        "ssh_host": "127.0.0.1",
        "ssh_port": 22,
    }).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    },
)
try:
    resp = urllib.request.urlopen(req)
    data = json.load(resp)
    print(data["id"])
except urllib.error.HTTPError as e:
    body = e.read().decode()
    if "already exists" in body:
        sys.stderr.write("Machine 'localhost-test' already exists — delete it first or run teardown.\n")
    else:
        sys.stderr.write(f"Register failed: {e.code} {body}\n")
    sys.exit(1)
PYEOF
)
echo "MACHINE_ID=$MACHINE_ID" >> "$STATE_FILE"
green "    Machine registered (id=$MACHINE_ID)."

# ── 8. Create test scripts ────────────────────────────────────────────────────
cyan ">>> Creating test scripts..."

for SCRIPT_NAME in hello counter sysinfo; do
    CONTENT=$(cat "$SCRIPTS_DIR/$SCRIPT_NAME.sh")
    META=$(cat "$SCRIPTS_DIR/$SCRIPT_NAME.meta.yaml")

    python3 - "$SCRIPT_NAME" "$SCRIPTS_DIR/$SCRIPT_NAME.sh" "$SCRIPTS_DIR/$SCRIPT_NAME.meta.yaml" "$SERVER_URL" "$TOKEN" <<'PYEOF'
import urllib.request, json, sys
name, sh_path, meta_path, server, token = sys.argv[1:6]
content = open(sh_path).read()
meta    = open(meta_path).read()
req = urllib.request.Request(
    f"{server}/api/scripts",
    data=json.dumps({"name": name, "content": content, "meta_yaml": meta}).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
)
try:
    urllib.request.urlopen(req)
    print(f"    Script '{name}' created.")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    if "already exists" in body:
        print(f"    Script '{name}' already exists — skipping.")
    else:
        sys.stderr.write(f"Script create failed: {e.code} {body}\n")
        sys.exit(1)
PYEOF
done

# ── Done ──────────────────────────────────────────────────────────────────────
echo
green "=== Setup complete! ==="
echo
echo "  Browser:       $SERVER_URL"
echo "  Machine URL:   $SERVER_URL/machines/$MACHINE_ID"
echo "  Agent log:     tail -f /tmp/csl_agent.log"
echo "  State file:    $STATE_FILE"
echo
bold "Next: open the checklist in another terminal:"
echo "  cat tests/manual/linux/web_test/CHECKLIST.md"
echo
echo "Teardown when done:"
echo "  bash tests/manual/linux/web_test/teardown.sh"
