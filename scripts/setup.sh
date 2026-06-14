#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# control-station-lite
# Copyright (C) 2026 Michal Dvořák
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version, with an additional permission for
# distribution through app stores (see LICENSE).

# One-shot bootstrap for the control station on a NAS / Linux host.
#
# Idempotent: rerunning is safe and never destroys data. Existing secrets,
# certificates and the database are left untouched; only missing pieces are
# created. See docs/dev/ARCHITECTURE.md §9.4.
#
# Overridable via environment (defaults in parentheses) — used by the bats
# suite to run against temp dirs with stubbed docker/systemctl:
#   CSL_DATA_DIR      (/var/lib/control-station-lite)  persistent state root
#   CSL_INSTALL_DIR   (/opt/control-station-lite)      staged deploy files
#   CSL_SYSTEMD_DIR   (/etc/systemd/system)            unit install location
#   CSL_CERT_HOSTNAME (prompted)                        CN for the self-signed cert
#   CSL_CONTAINER_UID (999)                             uid:gid the app container runs as
#   DOCKER / SYSTEMCTL / OPENSSL                        tool overrides
set -euo pipefail

DATA_DIR="${CSL_DATA_DIR:-/var/lib/control-station-lite}"
INSTALL_DIR="${CSL_INSTALL_DIR:-/opt/control-station-lite}"
SYSTEMD_DIR="${CSL_SYSTEMD_DIR:-/etc/systemd/system}"
# The prod image runs as a non-root user with this pinned uid/gid (see
# deploy/Dockerfile). The bind-mounted data dirs must be owned by it so the
# container can read the master key and write the database.
CONTAINER_UID="${CSL_CONTAINER_UID:-999}"
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

# Give the non-root app container (uid ${CONTAINER_UID}) ownership of the dirs
# and files it must read/write through the bind mounts. Run AFTER secrets/certs
# are generated so the files themselves (not just the dirs) are reassigned.
# certs/ is left root-owned — nginx's master process runs as root and reads it.
fix_ownership() {
    log "Setting data ownership to uid ${CONTAINER_UID} (non-root container user)"
    chown -R "${CONTAINER_UID}:${CONTAINER_UID}" \
        "${DATA_DIR}/db" "${DATA_DIR}/scripts" "${DATA_DIR}/secrets" "${DATA_DIR}/logs" \
        || warn "could not chown data dirs to ${CONTAINER_UID} (run as root) — the container may fail to read the master key or write the database"
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

# --- 8. built-in script catalogue ------------------------------------------
seed_scripts() {
    # Idempotent (create-if-absent): never clobbers admin edits. Needs an admin
    # row to attribute the scripts to, so it runs after create_admin.
    log "Seeding the built-in script catalogue"
    "${DOCKER}" compose -f "${INSTALL_DIR}/deploy/docker-compose.yml" \
        exec -T app csl-admin seed-scripts \
        || warn "seed-scripts skipped/failed — run csl-admin seed-scripts later (needs an admin)"
}

# --- 9. summary ------------------------------------------------------------
print_summary() {
    local host="${CSL_CERT_HOSTNAME:-$(hostname)}"
    log "Done. The control station should be reachable at: https://${host}/"
}

main() {
    check_deps
    ensure_dirs
    generate_secrets
    generate_cert
    fix_ownership
    stage_files
    install_service
    create_admin
    seed_scripts
    print_summary
}

main "$@"
