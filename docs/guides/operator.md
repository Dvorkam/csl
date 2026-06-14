# Operator guide

For people who deploy and run the control station process itself — typically on a
NAS or always-on home server. (For managing the script library, users, and
machines once it's running, see the [admin guide](admin.md).)

---

## Deploying with Docker (recommended)

From a checkout on the host:

```bash
sudo scripts/setup.sh
```

`setup.sh` is **idempotent** — rerunning is safe and never destroys data. It:

1. Checks dependencies (Docker, systemd, openssl).
2. Creates the data tree under `/var/lib/control-station-lite/` (`db`, `scripts`,
   `secrets`, `certs`, `logs`); `secrets/` is `chmod 700`.
3. Generates secrets **if absent** (never overwritten): `secrets/master.key`
   (base64 AES-256 key) and `secrets/jwt.key`.
4. Generates a self-signed TLS cert **if absent**: `certs/fullchain.pem` +
   `certs/privkey.pem` (replace with a real cert later — see
   [TLS certificates](#replacing-tls-certificates)).
5. Stages the deploy files to `/opt/control-station-lite/`.
6. Installs and starts the systemd unit (`control-station.service`), which drives
   `docker compose` from `/opt/control-station-lite/deploy`.
7. Creates the first admin interactively and seeds the built-in script catalogue.

Migrations run automatically on every container start (`alembic upgrade head`,
idempotent), so the schema is always current.

The stack runs the app behind **nginx**, which is the sole network edge (TLS
termination, HTTP→HTTPS redirect, rate limiting on `/api/auth/*`, request-size
limits). The app port is never published directly.

### Paths at a glance

| Path | Contents |
| --- | --- |
| `/var/lib/control-station-lite/db/control-station.sqlite` | The SQLite database |
| `/var/lib/control-station-lite/secrets/master.key` | AES-256 master key (encrypts stored SSH keys + agent tokens) |
| `/var/lib/control-station-lite/secrets/jwt.key` | JWT signing key |
| `/var/lib/control-station-lite/certs/{fullchain,privkey}.pem` | TLS cert + key |
| `/opt/control-station-lite/deploy/` | Staged compose files; where the systemd unit runs `docker compose` |

### Operating the service

```bash
systemctl status control-station          # service state
systemctl restart control-station         # restart the stack
journalctl -u control-station -f          # follow logs (structured JSON, one object/line)

# act inside the running app container:
cd /opt/control-station-lite/deploy
docker compose exec app csl-admin create-admin
docker compose exec -T app csl-admin seed-scripts
```

---

## Deploying on Synology / non-systemd / odd hosts

`setup.sh` assumes a standard Linux host: `docker compose` v2, systemd, Docker at
`/usr/bin/docker`, free ports 80/443, and writable `/var/lib` + `/opt`. On a
Synology NAS (DSM 7) and similar appliances several of those don't hold, so deploy
the same stack by hand. The pieces are identical — only the paths, ports, and
service management differ.

What's different on DSM 7 and how to handle it:

| Assumption in `setup.sh` | Reality on Synology DSM 7 | Fix |
| --- | --- | --- |
| `docker compose` v2 plugin | only `docker-compose` v1 ships | install the v2 plugin into `/usr/local/lib/docker/cli-plugins/` |
| Docker at `/usr/bin/docker` | it's at `/usr/local/bin/docker` | run compose by hand; skip the systemd unit |
| ports 80/443 free | DSM's own nginx owns them | remap the compose `nginx` ports, e.g. `8080:80` / `8443:443` |
| data on `/var/lib`, code on `/opt` | system volume `/` is tiny (~2 GB) | put everything under `/volume1/...` |
| normal root `umask` (022) | DSM root `umask` is **077** → everything `700` | see the two permission notes below |

A complete manual deployment:

```bash
# 1. install the docker compose v2 plugin (system-wide, so root/sudo sees it)
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
sudo docker compose version          # confirm v2

# 2. clone + data dirs on the big volume
sudo git clone https://github.com/Dvorkam/csl.git /volume1/control-station-lite/src
sudo mkdir -p /volume1/control-station-lite/{db,scripts,secrets,certs,logs}
sudo chmod 700 /volume1/control-station-lite/secrets

# 3. secrets + a TLS cert (add every IP/name you'll browse from as a SAN)
sudo sh -c 'openssl rand -base64 32 > /volume1/control-station-lite/secrets/master.key'
sudo sh -c 'openssl rand -base64 64 > /volume1/control-station-lite/secrets/jwt.key'
sudo openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /volume1/control-station-lite/certs/privkey.pem \
  -out    /volume1/control-station-lite/certs/fullchain.pem \
  -days 825 -subj "/CN=control-station-lite" \
  -addext "subjectAltName=IP:192.168.1.50,IP:100.64.0.1"   # your LAN + Tailscale IPs

# 4. point the compose file at /volume1 and remap the ports
sudo sed -i \
  -e 's#/var/lib/control-station-lite#/volume1/control-station-lite#g' \
  -e 's#"80:80"#"8080:80"#' -e 's#"443:443"#"8443:443"#' \
  /volume1/control-station-lite/src/deploy/docker-compose.yml

# 5. CRITICAL: a `sudo git clone` under umask 077 leaves every file mode 600,
#    which Docker COPY bakes into the image (entrypoint/alembic.ini unreadable by
#    the non-root container user). Make the build context world-readable first.
sudo chmod -R a+rX /volume1/control-station-lite/src

# 6. CRITICAL: the app container runs as a non-root user (uid 999). Give it the
#    bind-mounted data dirs so it can read the master key and write the DB.
sudo chown -R 999:999 /volume1/control-station-lite/{db,scripts,secrets,logs}

# 7. build + run (no systemd; `restart: unless-stopped` handles boot autostart)
sudo docker compose -f /volume1/control-station-lite/src/deploy/docker-compose.yml up -d --build

# 8. verify, then create the admin and seed
sudo docker compose -f /volume1/control-station-lite/src/deploy/docker-compose.yml ps
curl -k https://localhost:8443/healthz
sudo docker compose -f /volume1/control-station-lite/src/deploy/docker-compose.yml exec app csl-admin create-admin
sudo docker compose -f /volume1/control-station-lite/src/deploy/docker-compose.yml exec -T app csl-admin seed-scripts
```

Browse to `https://<nas-ip>:8443/`. **Autostart on reboot** is handled by the
`restart: unless-stopped` policy in the compose file — Synology's Docker package
starts at boot and restarts those containers — so no systemd unit is needed.
**Upgrades:** `sudo git -C /volume1/control-station-lite/src pull`, redo steps 4–6
on the pulled tree, then `up -d --build`.

> Steps 5 and 6 are the two non-obvious ones. They are normally done for you by
> `setup.sh` (it `chown`s the data dirs, and `deploy/Dockerfile` sets absolute
> file modes so a restrictive build-context umask can't break the image) — but a
> manual deploy must do them explicitly.

---

## Running without Docker

Install the server profile and provide the secrets/config yourself:

```bash
pip install control-station-lite[server]

openssl rand -hex 64 > secrets/jwt.key
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())" > secrets/master.key

export CSL_JWT_KEY_PATH=secrets/jwt.key
export CSL_MASTER_KEY_PATH=secrets/master.key
export CSL_DATABASE_URL=sqlite+aiosqlite:///data/control-station.sqlite   # default

alembic upgrade head
csl-admin create-admin
csl-server
```

| Setting (env, `CSL_` prefix) | Default | Notes |
| --- | --- | --- |
| `CSL_MASTER_KEY_PATH` | — (required) | File holding the base64 32-byte AES key. |
| `CSL_JWT_KEY_PATH` | — (required) | File holding the JWT signing key. |
| `CSL_DATABASE_URL` | `sqlite+aiosqlite:///data/control-station.sqlite` | SQLAlchemy async URL. |
| `CSL_HOST` | `127.0.0.1` | Bind address (keep behind a reverse proxy). |
| `CSL_PORT` | `8000` | App port. |
| `CSL_LOG_LEVEL` | `INFO` | |
| `CSL_COOKIE_SECURE` | `true` | Set `false` only for plain-HTTP localhost dev. |

The server fails loudly at startup if a required secret is missing or malformed
(e.g. a master key that isn't exactly 32 bytes).

---

## Backing up

Two things must be backed up **together**, or the backup is useless:

1. **The database** — `/var/lib/control-station-lite/db/control-station.sqlite`.
2. **The secrets** — `/var/lib/control-station-lite/secrets/`. The DB stores SSH
   private keys and agent tokens **encrypted with `master.key`**. A database
   without its master key cannot be decrypted; a master key without its database
   is meaningless. Keep both, and store the master key with at least the care you
   give the database.

Back up the SQLite file with a consistent snapshot rather than a naive copy of a
file being written:

```bash
# online, consistent copy
sqlite3 /var/lib/control-station-lite/db/control-station.sqlite \
  ".backup '/path/to/backup/control-station-$(date +%F).sqlite'"

# secrets (treat as sensitive)
tar czf /path/to/backup/secrets-$(date +%F).tgz \
  -C /var/lib/control-station-lite secrets
```

To restore: put the SQLite file back, restore the matching `secrets/`, run
`alembic upgrade head` (idempotent), and start the service.

> The audit log table grows unbounded — there is no pruning yet. Factor that into
> long-term backup sizing.

---

## Rotating the master key

The master key (`secrets/master.key`) encrypts two columns in the `machines`
table: `ssh_key_encrypted` (the per-machine SSH private key) and
`agent_token_encrypted` (the agent API bearer token). Rotation means decrypting
those with the old key and re-encrypting with a new one. There is **no built-in
rotate command**, so do it deliberately.

**Option A — re-register (simplest, no DB surgery).** Generate a new master key,
point the service at it, and re-register each machine from a fresh registration
bundle. Old rows can't be decrypted under the new key, so this is a clean reset
when you have only a few machines.

**Option B — re-encrypt in place.** Stop the service, then re-encrypt the two
columns with a short script that uses the project's own crypto helpers:

```python
# re-encrypt machines.* from OLD_KEY to NEW_KEY, then swap master.key
import base64, sqlite3
from control_station_lite.server.core.crypto import decrypt, encrypt

OLD = base64.b64decode(open("secrets/master.key.old", "rb").read().strip())
NEW = base64.b64decode(open("secrets/master.key.new", "rb").read().strip())

db = sqlite3.connect("/var/lib/control-station-lite/db/control-station.sqlite")
for row_id, ssh_enc, tok_enc in db.execute(
    "SELECT id, ssh_key_encrypted, agent_token_encrypted FROM machines"
).fetchall():
    new_ssh = encrypt(decrypt(ssh_enc, OLD), NEW)
    new_tok = encrypt(decrypt(tok_enc, OLD), NEW) if tok_enc is not None else None
    db.execute(
        "UPDATE machines SET ssh_key_encrypted=?, agent_token_encrypted=? WHERE id=?",
        (new_ssh, new_tok, row_id),
    )
db.commit()
db.close()
```

Then replace `secrets/master.key` with the new key and restart. **Back up the DB
and the old key before you start** — a mistake here orphans every machine's
credentials.

> Rotating the master key does **not** rotate the JWT key. Replacing
> `secrets/jwt.key` invalidates all existing sessions (everyone is logged out),
> which is harmless — users simply log in again.

---

## Replacing TLS certificates

`setup.sh` generates a self-signed cert so the stack comes up on HTTPS
immediately. To use a real certificate (e.g. from your internal CA or Let's
Encrypt), replace the two files nginx reads and restart:

```bash
cp your-fullchain.pem /var/lib/control-station-lite/certs/fullchain.pem
cp your-privkey.pem   /var/lib/control-station-lite/certs/privkey.pem
chmod 600 /var/lib/control-station-lite/certs/privkey.pem
systemctl restart control-station
```

nginx bind-mounts `certs/` read-only, so a restart is enough to pick up new
files. Keep the filenames as `fullchain.pem` / `privkey.pem` unless you also edit
the nginx config.

---

## Upgrading

```bash
cd <checkout>
git pull
sudo scripts/setup.sh        # idempotent: keeps data, secrets, certs; restages + restarts
```

Migrations run automatically on container start, so a normal upgrade needs no
manual `alembic` step. The control station refuses to register an agent whose
**major** version differs from its own, so keep server and agents on compatible
majors when upgrading across a major boundary.

---

## Security model in brief

What you're operating, security-wise (full detail in
[`docs/dev/ARCHITECTURE.md`](../dev/ARCHITECTURE.md) §7):

- nginx is the only network edge; the app is never published directly.
- All control-station ↔ agent traffic rides an SSH tunnel; no agent port is ever
  network-exposed.
- The control station's SSH key on each target is locked to a forced command and
  cannot get a shell.
- Agent host keys are pinned at registration (TOFU) and validated on every
  connection; mismatches fail closed.
- Every agent request carries a bearer token; the agent rejects unauthenticated
  calls on all endpoints.
- Stored SSH keys and agent tokens are AES-256-GCM encrypted with the master key.
- Passwords are bcrypt-hashed; auth uses short-lived JWT access tokens plus a
  rotating, revocable refresh token in an `HttpOnly` cookie.
