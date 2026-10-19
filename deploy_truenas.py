#!/usr/bin/env python3
"""Install or upgrade wllharvest as a TrueNAS SCALE custom app.

Run as root on the NAS from a copy of this directory (it needs compose.yaml and
wllharvest.py beside it):

    sudo python3 deploy_truenas.py [--dataset SSD/WeatherLink] [--app wllharvest]

Safe to re-run; that is also how to upgrade. It
  1. creates the dataset with TrueNAS's APPS preset if it does not exist,
  2. gives the user running sudo inheritable read access to the dataset,
  3. installs wllharvest.py into <dataset>/code (read-only in the container),
  4. makes <dataset>/data writable by the apps user (568) and checks access,
  5. creates the app from compose.yaml with the real paths; for an existing
     app it updates the compose config and/or restarts it only if the config
     or code changed, so a no-change run costs no data, and
  6. waits until status.json shows polls (and UDP packets, if enabled).
Written against the TrueNAS 25.10 API.
"""

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
APPS_ID = 568                                  # TrueNAS 'apps' user and group
PLACEHOLDER = "/mnt/POOL/apps/wllharvest"      # paths in compose.yaml to replace


def can(uid, gid, *cmd):
    """True if cmd succeeds when run as uid:gid with no supplementary groups."""
    return subprocess.run(["setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", *cmd],
                          capture_output=True).returncode == 0


def grant_read(c, path, uid):
    """Add an inheritable READ entry for uid to path's NFSv4 ACL, applied
    recursively so files already written become readable too."""
    acl = c.call("filesystem.getacl", str(path))
    if acl["acltype"] != "NFS4":
        return f"skipped ({acl['acltype']} ACL)"
    if any(e["tag"] == "USER" and e.get("id") == uid and e["type"] == "ALLOW" for e in acl["acl"]):
        return "already present"
    dacl = acl["acl"] + [{"tag": "USER", "id": uid, "type": "ALLOW",
                          "perms": {"BASIC": "READ"}, "flags": {"BASIC": "INHERIT"}}]
    c.call("filesystem.setacl", {"path": str(path), "dacl": dacl, "options": {"recursive": True}},
           job=True, timeout=600)
    return "added"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default="SSD/WeatherLink")
    p.add_argument("--app", default="wllharvest")
    p.add_argument("--wait", type=float, default=120, help="s to wait for fresh data")
    p.add_argument("--no-app", action="store_true",
                   help="only set up the dataset, permissions and code; leave the app untouched")
    a = p.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root: sudo python3 deploy_truenas.py")
    compose = (HERE / "compose.yaml").read_text()
    volumes = {f"{PLACEHOLDER}/code:/app:ro": "code", f"{PLACEHOLDER}/data:/data": "data"}
    if any(f"- {v}" not in compose for v in volumes):
        sys.exit(f"compose.yaml must mount each of {list(volumes)}")
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

        new_code = (HERE / "wllharvest.py").read_bytes()
        target = code / "wllharvest.py"
        code_changed = not target.exists() or target.read_bytes() != new_code
        if code_changed:
            tmp = code / ".wllharvest.py.tmp"
            tmp.write_bytes(new_code)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        print(f"{'installed' if code_changed else 'unchanged'} {target}; data in {data}")

        probe = data / ".write_probe"
        checks = [
            ("apps user can write data/", True,
             can(APPS_ID, APPS_ID, "sh", "-c", f"echo ok > '{probe}' && rm '{probe}'")),
            ("apps user can read the code", True, can(APPS_ID, APPS_ID, "cat", str(target))),
        ]
        if user:
            st_file = data / "status.json"
            checks.append((f"uid {user[0]} can read data/", False, can(
                *user, "sh", "-c", f"ls '{data}' >/dev/null && {{ [ ! -e '{st_file}' ] || cat '{st_file}' >/dev/null; }}")))
        for what, required, ok in checks:
            print(f"  {'ok  ' if ok else 'FAIL'} {what}{'' if ok or required else ' (not required)'}")
        if not all(ok for _, required, ok in checks if required):
            sys.exit("permission check failed; app not installed or changed")

        if a.no_app:
            print("--no-app: app left untouched")
            return
        for v, sub in volumes.items():
            compose = compose.replace(v, v.replace(PLACEHOLDER + "/" + sub, str(mnt / sub)))
        restarted = True
        if c.call("app.query", [["name", "=", a.app]]):
            compose_changed = c.call("app.config", a.app) != yaml.safe_load(compose)
            if compose_changed:   # TrueNAS force-recreates every container, loading the new code too
                c.call("app.update", a.app, {"custom_compose_config_string": compose}, job=True, timeout=900)
                print(f"updated app {a.app} (compose config changed)")
            elif code_changed:
                c.call("app.redeploy", a.app, job=True, timeout=900)
                print(f"redeployed app {a.app} to load the new code")
            restarted = compose_changed or code_changed
            if not restarted:
                print(f"app {a.app} unchanged, left running")
        else:
            c.call("app.create", {"custom_app": True, "app_name": a.app,
                                  "custom_compose_config_string": compose}, job=True, timeout=900)
            print(f"created app {a.app}")
        app = c.call("app.query", [["name", "=", a.app]], {"select": ["name", "state"]})
        print(f"app state: {app[0]['state'] if app else 'missing?'}")

    # The harvester rewrites status.json after every poll. After a (re)start,
    # wait for the new process's; otherwise just for a fresh update.
    status, st, fresh = data / "status.json", None, False
    deadline = time.time() + a.wait
    while not fresh and time.time() < deadline:
        time.sleep(3)
        try:
            st = json.loads(status.read_text())
            started = dt.datetime.fromisoformat(st["started"]).timestamp()
        except (OSError, ValueError, KeyError):
            continue
        new_enough = started >= t_start - 5 if restarted else st["updated_epoch"] >= t_start
        fresh = new_enough and st["http"]["ok"] >= 2 \
            and (not st["udp"]["enabled"] or st["udp"]["packets"] >= 3)
    if st:
        print(json.dumps({"did": st["did"], "started": st["started"], "http": st["http"],
                          "udp": st["udp"]}, indent=1))
    if not fresh:
        sys.exit(f"no fresh data within {a.wait:.0f} s: check the app's logs in the TrueNAS UI")
    print("deployed and harvesting")
    port = (yaml.safe_load(compose)["services"].get("web", {}).get("environment") or {}).get("WLL_SERVE_PORT")
    if port:
        import urllib.request
        for _ in range(10):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    print(f"web service on port {port}: {r.read().decode().strip()}")
                    break
            except OSError as e:
                err = e
                time.sleep(3)
        else:
            print(f"web service on port {port} not answering: {err}")


if __name__ == "__main__":
    main()
