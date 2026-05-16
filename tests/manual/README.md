# Manual tests

Scripts that verify behaviour which depends on real OS state — a running SSH
daemon, systemd, Task Scheduler, icacls — and therefore cannot run in CI.

Run them once after first-time setup on a target machine, and again after
any change to the functions listed in each script's header.

## How to run

```bash
# from the repo root
python tests/manual/linux/check_readiness.py
python tests/manual/linux/check_service_install.py
python tests/manual/linux/check_authorized_keys.py
```

```powershell
# from the repo root (Windows, run as the user you'll register)
python tests/manual/windows/check_readiness.py
python tests/manual/windows/check_admin_authorized_keys.py
python tests/manual/windows/check_service_install.py
```

Exit code 0 = all checks passed.  Non-zero = at least one check failed.

## When to run

| Script | Re-run when |
| --- | --- |
| `check_readiness` | `check_readiness()`, `_sshd_running_*`, `setup_system()` changes |
| `check_authorized_keys` | `_append_authorized_keys`, `_windows_is_admin`, `_set_admin_ak_acl` changes |
| `check_service_install` | `install_service()`, `service_installer.py` changes |

## What these scripts do NOT do

- They do not install or remove packages.
- They do not start or stop system services (except `check_readiness` which
  only reads state, never writes it).
- `check_service_install` writes the service unit / task definition, which is
  exactly what production does.  It offers to clean up at the end.
