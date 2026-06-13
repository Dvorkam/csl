#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# One-shot bootstrap for the control station on a NAS / Linux host.
#
# Idempotent: rerunning is safe and never destroys data. Existing secrets,
# certificates and the database are left untouched; only missing pieces are
# created. See docs/ARCHITECTURE.md §9.4.
#
# Overridable via environment (defaults in parentheses) — used by the bats
# suite to run against temp dirs with stubbed docker/systemctl:
#   CSL_DATA_DIR      (/var/lib/control-station-lite)  persistent state root
#   CSL_INSTALL_DIR   (/opt/control-station-lite)      staged deploy files
#   CSL_SYSTEMD_DIR   (/etc/systemd/system)            unit install location
#   CSL_CERT_HOSTNAME (prompted)                        CN for the self-signed cert
#   DOCKER / SYSTEMCTL / OPENSSL                        tool overrides
set -euo pipefail

DATA_DIR="${CSL_DATA_DIR:-/var/lib/control-station-lite}"
INSTALL_DIR="${CSL_INSTALL_DIR:-/opt/control-station-lite}"
SYSTEMD_DIR="${CSL_SYSTEMD_DIR:-/etc/systemd/system}"
DOCKER="${DOCKER:-docker}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
OPENSSL="${OPENSSL:-openssl}"

# Repo root, derived from this script's location (scripts/setup.sh).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

log()  { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. dependencies -------------------------------------------------------
check_deps() {
    command -v "${DOCKER}" >/dev/null 2>&1 || die "docker not found — install Docker first"
    "${DOCKER}" compose version >/dev/null 2>&1 \
        || die "'docker compose' not available — install the Compose plugin"
    command -v "${OPENSSL}" >/dev/null 2>&1 || die "openssl not found"
    command -v "${SYSTEMCTL}" >/dev/null 2>&1 \
        || warn "systemctl not found — skipping service install (run the stack manually)"
}

# --- 2. data directories ---------------------------------------------------
ensure_dirs() {
    log "Ensuring data directories under ${DATA_DIR}"
    local sub
    for sub in db scripts secrets certs logs; do
        mkdir -p "${DATA_DIR}/${sub}"
    done
    chmod 700 "${DATA_DIR}/secrets"
}

# --- 3. secrets (never overwritten) ----------------------------------------
generate_secrets() {
    local master="${DATA_DIR}/secrets/master.key"
    local jwt="${DATA_DIR}/secrets/jwt.key"
    if [ -f "${master}" ]; then
        log "master.key exists — keeping it"
    else
        log "Generating master.key (AES-256 key, base64)"
        "${OPENSSL}" rand -base64 32 >"${master}"
        chmod 600 "${master}"
    fi
    if [ -f "${jwt}" ]; then
        log "jwt.key exists — keeping it"
    else
        log "Generating jwt.key"
        "${OPENSSL}" rand -base64 64 >"${jwt}"
        chmod 600 "${jwt}"
    fi
}

# --- 4. self-signed certificate (never overwritten) ------------------------
generate_cert() {
    local cert="${DATA_DIR}/certs/fullchain.pem"
    local key="${DATA_DIR}/certs/privkey.pem"
    if [ -f "${cert}" ] && [ -f "${key}" ]; then
        log "TLS certificate exists — keeping it"
        return
    fi
    local cn="${CSL_CERT_HOSTNAME:-}"
    if [ -z "${cn}" ]; then
        if [ -t 0 ]; then
            printf 'Hostname for the self-signed TLS certificate [%s]: ' "$(hostname)"
            read -r cn
        fi
        cn="${cn:-$(hostname)}"
    fi
    log "Generating self-signed certificate for CN=${cn} (replace with a real cert later)"
    "${OPENSSL}" req -x509 -newkey rsa:2048 -nodes \
        -keyout "${key}" -out "${cert}" \
        -days 825 -subj "/CN=${cn}" >/dev/null 2>&1
    chmod 600 "${key}"
}

# --- 5. stage deploy files -------------------------------------------------
stage_files() {
    log "Staging deploy files at ${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}"
    # Copy the repo (sans .git) so the compose build context (..) resolves. Once
    # the release image is published (Task 10.8) this becomes a pull instead.
    tar -C "${REPO_ROOT}" --exclude=./.git -cf - . | tar -C "${INSTALL_DIR}" -xf -
}

# --- 6. systemd unit -------------------------------------------------------
install_service() {
    if ! command -v "${SYSTEMCTL}" >/dev/null 2>&1; then
        warn "systemctl unavailable — not installing the service"
        return
    fi
    log "Installing systemd unit"
    cp "${REPO_ROOT}/deploy/control-station.service" \
        "${SYSTEMD_DIR}/control-station.service"
    "${SYSTEMCTL}" daemon-reload
    "${SYSTEMCTL}" enable control-station.service
    "${SYSTEMCTL}" restart control-station.service
}

# --- 7. initial admin user -------------------------------------------------
create_admin() {
    # `csl-admin create-admin` is interactive and refuses to clobber an existing
    # username, so it is safe to offer on every run. Skip when there is no TTY
    # (unattended rerun / CI) — the admin can be created later with:
    #   docker compose -f docker-compose.yml exec app csl-admin create-admin
    if [ ! -t 0 ]; then
        log "Non-interactive run — skipping admin creation (create it later via csl-admin create-admin)"
        return
    fi
    log "Creating the initial admin user (Ctrl-C to skip if one already exists)"
    "${DOCKER}" compose -f "${INSTALL_DIR}/deploy/docker-compose.yml" \
        exec app csl-admin create-admin \
        || warn "admin creation skipped/failed — run csl-admin create-admin later"
}

# --- 8. summary ------------------------------------------------------------
print_summary() {
    local host="${CSL_CERT_HOSTNAME:-$(hostname)}"
    log "Done. The control station should be reachable at: https://${host}/"
}

main() {
    check_deps
    ensure_dirs
    generate_secrets
    generate_cert
    stage_files
    install_service
    create_admin
    print_summary
}

main "$@"
