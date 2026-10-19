#!/usr/bin/env python3
"""Install or upgrade wllharvest as a TrueNAS SCALE custom app.

Run as root on the NAS from a copy of this directory (it needs compose.yaml,
wllharvest.py and web/ beside it):

    sudo python3 deploy_truenas.py [--dataset SSD/WeatherLink] [--app wllharvest]

Safe to re-run; that is also how to upgrade. It
  1. creates the dataset with TrueNAS's APPS preset if it does not exist,
  2. gives the user running sudo inheritable read access to the dataset,
  3. installs wllharvest.py and web/ into <dataset>/code (read-only in the containers),
  4. makes <dataset>/data writable by the apps user (568) and checks access,
  5. creates the app from compose.yaml with the real paths; for an existing
     app it updates the compose config and/or restarts it only if the config
     or code changed, so a no-change run costs no data, and
  6. waits until status.json shows polls (and UDP packets, if enabled).
Written against the TrueNAS 25.10 API. Runs on the NAS's own Python (3.11).
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
APPS_ID = 568  # TrueNAS 'apps' user and group
PLACEHOLDER = "/mnt/POOL/apps/wllharvest"  # host paths in compose.yaml to replace
VOLUMES = {f"{PLACEHOLDER}/code:/app:ro": "code", f"{PLACEHOLDER}/data:/data": "data"}


def can(uid: int, gid: int, *cmd: str) -> bool:
    """True if cmd succeeds when run as uid:gid with no supplementary groups."""
    return (
        subprocess.run(
            ["setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", *cmd], capture_output=True, check=False
        ).returncode
        == 0
    )


def fill_paths(compose: str, mnt: Path) -> str:
    """compose.yaml with its placeholder host paths pointed into the dataset."""
    for v, sub in VOLUMES.items():
        if f"- {v}" not in compose:
            raise ValueError(f"compose.yaml must mount {v}")
        compose = compose.replace(v, v.replace(f"{PLACEHOLDER}/{sub}", str(mnt / sub)))
    return compose


def grant_read(c: Any, path: Path, uid: int) -> str:
    """Add an inheritable READ entry for uid to path's NFSv4 ACL, applied
    recursively so files already written become readable too."""
    acl = c.call("filesystem.getacl", str(path))
    if acl["acltype"] != "NFS4":
        return f"skipped ({acl['acltype']} ACL)"
    if any(e["tag"] == "USER" and e.get("id") == uid and e["type"] == "ALLOW" for e in acl["acl"]):
        return "already present"
    dacl = [
        *acl["acl"],
        {"tag": "USER", "id": uid, "type": "ALLOW", "perms": {"BASIC": "READ"}, "flags": {"BASIC": "INHERIT"}},
    ]
    c.call(
        "filesystem.setacl", {"path": str(path), "dacl": dacl, "options": {"recursive": True}}, job=True, timeout=600
    )
    return "added"


def code_files(src_root: Path) -> list[Path]:
    """What the containers run, relative to src_root: the harvester and the
    dashboard's web directory."""
    web = sorted(p.relative_to(src_root) for p in (src_root / "web").rglob("*") if p.is_file())
    return [Path("wllharvest.py"), *web]


def install_code(src_root: Path, code_dir: Path) -> bool:
    """Atomically install code_files() into code_dir; True if any changed."""
    changed = False
    for rel in code_files(src_root):
        new, target = (src_root / rel).read_bytes(), code_dir / rel
        if target.exists() and target.read_bytes() == new:
            continue
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(new)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
        changed = True
    return changed


def deploy_app(c: Any, app: str, compose: str, code_changed: bool) -> str:
    """Create the app, or bring an existing one up to date with as few
    restarts as possible. Returns "created", "updated", "redeployed" or
    "unchanged"."""
    if not c.call("app.query", [["name", "=", app]]):
        c.call(
            "app.create",
            {"custom_app": True, "app_name": app, "custom_compose_config_string": compose},
            job=True,
            timeout=900,
        )
        return "created"
    if c.call("app.config", app) != yaml.safe_load(compose):
        # TrueNAS force-recreates every container, which also loads the new code.
        c.call("app.update", app, {"custom_compose_config_string": compose}, job=True, timeout=900)
        return "updated"
    if code_changed:
        c.call("app.redeploy", app, job=True, timeout=900)
        return "redeployed"
    return "unchanged"


def wait_fresh(
    status: Path, t_start: float, restarted: bool, wait: float, every: float = 3.0
) -> tuple[dict[str, Any] | None, bool]:
    """Wait for status.json to show fresh polls (and UDP packets, if enabled).
    After a (re)start the status must come from the new process; otherwise
    an update since t_start is enough. Returns (last status read, fresh)."""
    st: dict[str, Any] | None = None
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(every)
        try:
            st = json.loads(status.read_text())
            started = dt.datetime.fromisoformat(st["started"]).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            continue
        new_enough = started >= t_start - 5 if restarted else st["updated_epoch"] >= t_start
        if new_enough and st["http"]["ok"] >= 2 and (not st["udp"]["enabled"] or st["udp"]["packets"] >= 3):
            return st, True
    return st, False


def web_port(compose: str) -> str | None:
    """WLL_SERVE_PORT of the compose file's web service, if it has one."""
    web = yaml.safe_load(compose)["services"].get("web") or {}
    port = (web.get("environment") or {}).get("WLL_SERVE_PORT")
    return None if port is None else str(port)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default="SSD/WeatherLink")
    p.add_argument("--app", default="wllharvest")
    p.add_argument("--wait", type=float, default=120, help="s to wait for fresh data")
    p.add_argument(
        "--no-app", action="store_true", help="only set up the dataset, permissions and code; leave the app untouched"
    )
    a = p.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root: sudo python3 deploy_truenas.py")
    compose = (HERE / "compose.yaml").read_text()
    try:
        fill_paths(compose, Path("/check"))
    except ValueError as e:
        sys.exit(str(e))
    user = None
    if os.environ.get("SUDO_UID"):
        user = int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", os.environ["SUDO_UID"]))
    from truenas_api_client import Client

    t_start = time.time()
    with Client() as c:
        ds = c.call("pool.dataset.query", [["id", "=", a.dataset]])
        if not ds:
            c.call("pool.dataset.create", {"name": a.dataset, "share_type": "APPS"})
            ds = c.call("pool.dataset.query", [["id", "=", a.dataset]])
            print(f"created dataset {a.dataset} (APPS preset)")
        mnt = Path(ds[0]["mountpoint"])
        code, data = mnt / "code", mnt / "data"
        code.mkdir(mode=0o755, exist_ok=True)
        data.mkdir(mode=0o755, exist_ok=True)
        os.chown(data, APPS_ID, APPS_ID)
        if user:
            print(f"read access for uid {user[0]}: {grant_read(c, mnt, user[0])}")

        code_changed = install_code(HERE, code)
        target = code / "wllharvest.py"
        print(f"code in {code}: {'updated' if code_changed else 'unchanged'}; data in {data}")

        probe, st_file = data / ".write_probe", data / "status.json"
        checks = [
            (
                "apps user can write data/",
                True,
                can(APPS_ID, APPS_ID, "sh", "-c", f"echo ok > '{probe}' && rm '{probe}'"),
            ),
            ("apps user can read the code", True, can(APPS_ID, APPS_ID, "cat", str(target))),
        ]
        if user:
            readable = f"ls '{data}' >/dev/null && {{ [ ! -e '{st_file}' ] || cat '{st_file}' >/dev/null; }}"
            checks.append((f"uid {user[0]} can read data/", False, can(*user, "sh", "-c", readable)))
        for what, required, ok in checks:
            print(f"  {'ok  ' if ok else 'FAIL'} {what}{'' if ok or required else ' (not required)'}")
        if not all(ok for _, required, ok in checks if required):
            sys.exit("permission check failed; app not installed or changed")

        if a.no_app:
            print("--no-app: app left untouched")
            return
        compose = fill_paths(compose, mnt)
        action = deploy_app(c, a.app, compose, code_changed)
        print(
            {
                "created": f"created app {a.app}",
                "updated": f"updated app {a.app} (compose config changed)",
                "redeployed": f"redeployed app {a.app} to load the new code",
                "unchanged": f"app {a.app} unchanged, left running",
            }[action]
        )
        app = c.call("app.query", [["name", "=", a.app]], {"select": ["name", "state"]})
        print(f"app state: {app[0]['state'] if app else 'missing?'}")

    st, fresh = wait_fresh(st_file, t_start, action != "unchanged", a.wait)
    if st:
        print(json.dumps({k: st.get(k) for k in ("did", "started", "http", "udp")}, indent=1))
    if not fresh:
        sys.exit(f"no fresh data within {a.wait:.0f} s: check the app's logs in the TrueNAS UI")
    print("deployed and harvesting")
    port = web_port(compose)
    if port:
        err: Exception | None = None
        for _ in range(10):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    print(f"web service on port {port}: {r.read().decode().strip()}")
                    return
            except OSError as e:
                err = e
                time.sleep(3)
        print(f"web service on port {port} not answering: {err}")


if __name__ == "__main__":
    main()
