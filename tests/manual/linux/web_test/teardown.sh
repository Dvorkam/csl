#!/bin/bash
# teardown.sh — Tears down the local CSL manual test environment.
#
# Reads state from /tmp/csl_web_test.env (written by setup.sh).
#
# Usage (from repo root):
#   bash tests/manual/linux/web_test/teardown.sh

set -euo pipefail

STATE_FILE="/tmp/csl_web_test.env"
REPO_DIR="$(cd "$(dirname "$0")/../../../.." && pwd)"
SERVER_URL="${CSL_SERVER:-http://127.0.0.1:16534}"

cyan()  { printf '\033[36m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

bold "=== CSL Web UI Test Teardown ==="
echo

if [ ! -f "$STATE_FILE" ]; then
    red "State file not found: $STATE_FILE"
    red "Either setup.sh was not run, or teardown was already performed."
    exit 1
fi

# Load state
# shellcheck source=/dev/null
source "$STATE_FILE"

# ── 1. Kill agent ─────────────────────────────────────────────────────────────
cyan ">>> Stopping csl-agent (PID ${AGENT_PID:-unknown})..."
if [ -n "${AGENT_PID:-}" ] && kill -0 "$AGENT_PID" 2>/dev/null; then
    kill "$AGENT_PID"
    sleep 1
    echo "    Agent stopped."
else
    # Fallback: kill by process name
    pkill -f "csl-agent$" 2>/dev/null && echo "    Agent stopped (by name)." || echo "    Agent was not running."
fi

# ── 2. Delete machine from control station ────────────────────────────────────
if [ -n "${MACHINE_ID:-}" ] && [ -n "${TOKEN:-}" ]; then
    cyan ">>> Deleting machine (id=$MACHINE_ID) from control station..."
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
        "$SERVER_URL/api/machines/$MACHINE_ID" \
        -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
    if [ "$STATUS" = "204" ]; then
        echo "    Machine deleted."
    elif [ "$STATUS" = "404" ]; then
        echo "    Machine already gone."
    else
        echo "    Warning: DELETE returned $STATUS (may need manual cleanup)."
    fi
fi

# ── 3. Delete test scripts from control station ───────────────────────────────
if [ -n "${TOKEN:-}" ]; then
    cyan ">>> Deleting test scripts..."
    for SCRIPT_NAME in hello counter sysinfo; do
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
            "$SERVER_URL/api/scripts/$SCRIPT_NAME" \
            -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
        if [ "$STATUS" = "204" ]; then
            echo "    Script '$SCRIPT_NAME' deleted."
        elif [ "$STATUS" = "404" ]; then
            echo "    Script '$SCRIPT_NAME' already gone."
        else
            echo "    Warning: DELETE '$SCRIPT_NAME' returned $STATUS."
        fi
    done
fi

# ── 4. Stop sshd if we started it ────────────────────────────────────────────
if [ "${SSHD_STARTED:-0}" = "1" ]; then
    cyan ">>> Stopping sshd (we started it during setup)..."
    sudo systemctl stop sshd
    echo "    sshd stopped."
else
    echo ">>> sshd was already running before setup — leaving it running."
fi

# ── 5. Purge orphaned approval entries (from fuzz tests etc.) ─────────────────
cyan ">>> Purging orphaned approval entries..."
cd "$REPO_DIR"
uv run csl-agent approvals purge 2>/dev/null && echo "    Done." || echo "    Skipped (no agent config)."

# ── 6. Clean up agent state ───────────────────────────────────────────────────
cyan ">>> Cleaning agent state..."
read -rp "    Remove ~/.csl/ agent config and keys? [y/N]: " CLEAN_AGENT
if [[ "${CLEAN_AGENT,,}" == "y" ]]; then
    rm -rf ~/.csl/config.yaml ~/.csl/agent/ ~/.csl/scripts/ ~/.csl/scripts.pending/ ~/.csl/logs/
    echo "    Agent state removed."
    echo "    Note: the CSL SSH key was left in ~/.ssh/authorized_keys."
    echo "          Remove it manually if desired."
else
    echo "    Skipped — ~/.csl/ left intact."
fi

# ── 7. Remove state file ──────────────────────────────────────────────────────
rm -f "$STATE_FILE"
echo

green "=== Teardown complete ==="
