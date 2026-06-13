#!/usr/bin/env bats
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Bats suite for scripts/setup.sh: fresh install, idempotent rerun, upgrade path.
#
# docker / systemctl are stubbed on PATH so the suite needs neither a daemon nor
# root. setup.sh is run with stdin redirected from /dev/null (no TTY), so the
# interactive admin-creation step is skipped automatically.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
    WORK="$(mktemp -d)"

    export CSL_DATA_DIR="${WORK}/data"
    export CSL_INSTALL_DIR="${WORK}/install"
    export CSL_SYSTEMD_DIR="${WORK}/systemd"
    export CSL_CERT_HOSTNAME="test.local"
    mkdir -p "${CSL_SYSTEMD_DIR}"

    # Stub external tools: record invocations, always succeed.
    STUB_BIN="${WORK}/bin"
    mkdir -p "${STUB_BIN}"
    cat >"${STUB_BIN}/docker" <<'EOF'
#!/bin/sh
exit 0
EOF
    cat >"${STUB_BIN}/systemctl" <<'EOF'
#!/bin/sh
exit 0
EOF
    chmod +x "${STUB_BIN}/docker" "${STUB_BIN}/systemctl"
    export PATH="${STUB_BIN}:${PATH}"
}

teardown() {
    rm -rf "${WORK}"
}

run_setup() {
    run bash "${REPO_ROOT}/scripts/setup.sh" </dev/null
}

@test "fresh install creates data directories" {
    run_setup
    [ "${status}" -eq 0 ]
    for sub in db scripts secrets certs logs; do
        [ -d "${CSL_DATA_DIR}/${sub}" ]
    done
}

@test "fresh install generates secrets and a self-signed cert" {
    run_setup
    [ "${status}" -eq 0 ]
    [ -s "${CSL_DATA_DIR}/secrets/master.key" ]
    [ -s "${CSL_DATA_DIR}/secrets/jwt.key" ]
    [ -s "${CSL_DATA_DIR}/certs/fullchain.pem" ]
    [ -s "${CSL_DATA_DIR}/certs/privkey.pem" ]
}

@test "master.key decodes to exactly 32 bytes" {
    run_setup
    [ "${status}" -eq 0 ]
    local n
    n="$(base64 -d <"${CSL_DATA_DIR}/secrets/master.key" | wc -c)"
    [ "${n}" -eq 32 ]
}

@test "rerun does not overwrite existing secrets or cert (idempotent)" {
    run_setup
    [ "${status}" -eq 0 ]
    local master_before jwt_before cert_before
    master_before="$(cat "${CSL_DATA_DIR}/secrets/master.key")"
    jwt_before="$(cat "${CSL_DATA_DIR}/secrets/jwt.key")"
    cert_before="$(cat "${CSL_DATA_DIR}/certs/fullchain.pem")"

    run_setup
    [ "${status}" -eq 0 ]
    [ "$(cat "${CSL_DATA_DIR}/secrets/master.key")" = "${master_before}" ]
    [ "$(cat "${CSL_DATA_DIR}/secrets/jwt.key")" = "${jwt_before}" ]
    [ "$(cat "${CSL_DATA_DIR}/certs/fullchain.pem")" = "${cert_before}" ]
}

@test "rerun preserves an existing database file (no data loss)" {
    run_setup
    [ "${status}" -eq 0 ]
    echo "sentinel" >"${CSL_DATA_DIR}/db/control-station.sqlite"

    run_setup
    [ "${status}" -eq 0 ]
    [ "$(cat "${CSL_DATA_DIR}/db/control-station.sqlite")" = "sentinel" ]
}

@test "install stages deploy files and the systemd unit" {
    run_setup
    [ "${status}" -eq 0 ]
    [ -f "${CSL_INSTALL_DIR}/deploy/docker-compose.yml" ]
    [ -f "${CSL_SYSTEMD_DIR}/control-station.service" ]
    # .git is excluded from the staged copy.
    [ ! -d "${CSL_INSTALL_DIR}/.git" ]
}

@test "upgrade re-stages newer deploy files while keeping data" {
    run_setup
    [ "${status}" -eq 0 ]
    local master_before
    master_before="$(cat "${CSL_DATA_DIR}/secrets/master.key")"
    # Simulate a stale staged file from a previous version.
    echo "stale" >"${CSL_INSTALL_DIR}/deploy/docker-compose.yml"

    run_setup
    [ "${status}" -eq 0 ]
    # Staged file refreshed from the repo...
    [ "$(cat "${CSL_INSTALL_DIR}/deploy/docker-compose.yml")" != "stale" ]
    # ...but data/secrets untouched.
    [ "$(cat "${CSL_DATA_DIR}/secrets/master.key")" = "${master_before}" ]
}
