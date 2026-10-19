"""Tests for deploy_truenas.py's decisions, against a fake TrueNAS API client.

These run on the NAS's own Python (3.11) as well as 3.13+, and need PyYAML,
which TrueNAS ships:  python3 -m unittest discover -s tests -p 'test_deploy*'
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    import yaml

    import deploy_truenas as dep
except ImportError:  # PyYAML missing; CI installs it
    yaml = None  # type: ignore[assignment]

COMPOSE = (ROOT / "compose.yaml").read_text()
MNT = Path("/mnt/SSD/WeatherLink")


class FakeClient:
    """Records calls; answers from a {method: value or callable} table."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def call(self, method: str, *params: Any, **kw: Any) -> Any:
        self.calls.append((method, params, kw))
        a = self.answers.get(method)
        return a(*params) if callable(a) else a

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls]


@unittest.skipUnless(yaml, "PyYAML not installed")
class DeployTests(unittest.TestCase):
    def test_fill_paths_points_both_services_into_the_dataset(self) -> None:
        services = yaml.safe_load(dep.fill_paths(COMPOSE, MNT))["services"]
        self.assertEqual(set(services), {"wllharvest", "web", "proxy"})
        for name in ("wllharvest", "web"):
            self.assertEqual(services[name]["volumes"], [f"{MNT}/code:/app:ro", f"{MNT}/data:/data"])
        with self.assertRaises(ValueError):
            dep.fill_paths(COMPOSE.replace(f"{dep.PLACEHOLDER}/data:/data", "/elsewhere:/data"), MNT)

    def test_new_app_is_created_with_the_filled_compose(self) -> None:
        compose = dep.fill_paths(COMPOSE, MNT)
        c = FakeClient({"app.query": []})
        self.assertEqual(dep.deploy_app(c, "wllharvest", compose, code_changed=True), "created")
        self.assertEqual(c.methods(), ["app.query", "app.create"])
        _, (args,), kw = c.calls[1]
        self.assertEqual(args, {"custom_app": True, "app_name": "wllharvest", "custom_compose_config_string": compose})
        self.assertTrue(kw["job"])

    def test_changed_compose_updates_once_without_a_second_restart(self) -> None:
        compose = dep.fill_paths(COMPOSE, MNT)
        old = yaml.safe_load(compose)
        old["services"]["wllharvest"]["environment"]["WLL_UDP"] = "1"
        c = FakeClient({"app.query": [{"name": "wllharvest"}], "app.config": old})
        self.assertEqual(dep.deploy_app(c, "wllharvest", compose, code_changed=True), "updated")
        self.assertEqual(c.methods(), ["app.query", "app.config", "app.update"])

    def test_code_only_change_redeploys(self) -> None:
        compose = dep.fill_paths(COMPOSE, MNT)
        c = FakeClient({"app.query": [{"name": "wllharvest"}], "app.config": yaml.safe_load(compose)})
        self.assertEqual(dep.deploy_app(c, "wllharvest", compose, code_changed=True), "redeployed")
        self.assertEqual(c.methods(), ["app.query", "app.config", "app.redeploy"])

    def test_no_change_leaves_the_app_running(self) -> None:
        compose = dep.fill_paths(COMPOSE, MNT)
        c = FakeClient({"app.query": [{"name": "wllharvest"}], "app.config": yaml.safe_load(compose)})
        self.assertEqual(dep.deploy_app(c, "wllharvest", compose, code_changed=False), "unchanged")
        self.assertEqual(c.methods(), ["app.query", "app.config"])

    def test_grant_read(self) -> None:
        base = [
            {
                "tag": "owner@",
                "id": None,
                "type": "ALLOW",
                "perms": {"BASIC": "FULL_CONTROL"},
                "flags": {"BASIC": "INHERIT"},
            }
        ]
        c = FakeClient({"filesystem.getacl": {"acltype": "NFS4", "acl": base}})
        self.assertEqual(dep.grant_read(c, MNT, 3000), "added")
        method, (args,), kw = c.calls[-1]
        self.assertEqual(method, "filesystem.setacl")
        self.assertEqual(args["dacl"][:-1], base)
        self.assertEqual(
            args["dacl"][-1],
            {"tag": "USER", "id": 3000, "type": "ALLOW", "perms": {"BASIC": "READ"}, "flags": {"BASIC": "INHERIT"}},
        )
        self.assertEqual(args["options"], {"recursive": True})
        self.assertTrue(kw["job"])
        present = FakeClient({"filesystem.getacl": {"acltype": "NFS4", "acl": args["dacl"]}})
        self.assertEqual(dep.grant_read(present, MNT, 3000), "already present")
        self.assertEqual(present.methods(), ["filesystem.getacl"])
        posix = FakeClient({"filesystem.getacl": {"acltype": "POSIX1E", "acl": []}})
        self.assertEqual(dep.grant_read(posix, MNT, 3000), "skipped (POSIX1E ACL)")

    def test_install_code_is_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src, code = Path(d) / "src", Path(d) / "code"
            (src / "web" / "vendor").mkdir(parents=True)
            code.mkdir()
            (src / "wllharvest.py").write_text("print('v1')\n")
            (src / "web" / "index.html").write_text("<p>v1</p>\n")
            (src / "web" / "vendor" / "lib.js").write_text("lib\n")
            self.assertEqual(
                [str(p) for p in dep.code_files(src)], ["wllharvest.py", "web/index.html", "web/vendor/lib.js"]
            )
            self.assertEqual(dep.install_code(src, code), ["wllharvest.py", "web/index.html", "web/vendor/lib.js"])
            self.assertEqual((code / "web" / "vendor" / "lib.js").read_text(), "lib\n")
            self.assertEqual((code / "wllharvest.py").stat().st_mode & 0o777, 0o644)
            self.assertEqual(dep.install_code(src, code), [])  # nothing changed: no restart needed
            (src / "web" / "index.html").write_text("<p>v2</p>\n")
            changed = dep.install_code(src, code)
            self.assertEqual(changed, ["web/index.html"])
            self.assertFalse(dep.needs_restart(changed))  # served from disk: takes effect at once
            (src / "wllharvest.py").write_text("print('v2')\n")
            self.assertTrue(dep.needs_restart(dep.install_code(src, code)))
            self.assertEqual(
                sorted(p.name for p in code.rglob("*")), ["index.html", "lib.js", "vendor", "web", "wllharvest.py"]
            )

    def test_wait_fresh_needs_the_new_process(self) -> None:
        t_start = time.time()
        status = {
            "started": "2026-10-19T17:45:40+00:00",
            "updated_epoch": t_start + 1,
            "did": "x",
            "http": {"ok": 5},
            "udp": {"enabled": False, "packets": 0},
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "status.json"
            path.write_text(json.dumps(status))
            self.assertFalse(dep.wait_fresh(path, t_start, True, wait=0.2, every=0.05)[1])  # old process
            self.assertTrue(dep.wait_fresh(path, t_start, False, wait=0.2, every=0.05)[1])  # no restart
            status["udp"] = {"enabled": True, "packets": 0}
            path.write_text(json.dumps(status))
            self.assertFalse(dep.wait_fresh(path, t_start, False, wait=0.2, every=0.05)[1])  # UDP pending

    def test_fill_paths_covers_the_proxy_certificates(self) -> None:
        services = yaml.safe_load(dep.fill_paths(COMPOSE, MNT))["services"]
        self.assertEqual(services["proxy"]["volumes"], [f"{MNT}/code:/app:ro", f"{MNT}/certs:/certs:ro"])
        self.assertFalse([v for s in services.values() for v in s.get("volumes", []) if "/mnt/POOL" in v])

    def test_proxy_network_is_consistent(self) -> None:
        c = yaml.safe_load(COMPOSE)
        lan = c["services"]["proxy"]["networks"]["lan"]
        ipam = c["networks"]["lan"]["ipam"]["config"][0]
        self.assertEqual(ipam["ip_range"], f"{lan['ipv4_address']}/32")
        self.assertTrue(int(lan["mac_address"].split(":")[0], 16) & 0b10)  # locally administered
        self.assertNotIn("network_mode", c["services"]["web"])  # macvlan cannot reach the host network
        traefik = yaml.safe_load((ROOT / "traefik.yml").read_text())
        cert = traefik["tls"]["stores"]["default"]["defaultCertificate"]
        self.assertEqual((cert["certFile"], cert["keyFile"]), ("/certs/weather.crt", "/certs/weather.key"))
        self.assertEqual(traefik["http"]["services"]["web"]["loadBalancer"]["servers"][0]["url"], "http://web:8580")

    def test_cert_sync_command_copies_only_changes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src, dest, bin_ = Path(d) / "etc", Path(d) / "certs", Path(d) / "bin"
            for p in (src, dest, bin_):
                p.mkdir()
            (src / "w.crt").write_text("cert 1\n")
            (src / "w.key").write_text("key 1\n")
            log = Path(d) / "docker.log"
            stub = bin_ / "docker"  # records calls; "ps" finds one proxy container
            stub.write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$1" = ps ] && echo abc123\nexit 0\n')
            stub.chmod(0o755)
            cmd = dep.cert_sync_command(
                str(src / "w.crt"), str(src / "w.key"), dest, "wllharvest", os.getuid(), os.getgid()
            )
            env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")

            def run() -> list[str]:
                log.write_text("")
                subprocess.run(["sh", "-c", cmd], env=env, check=False)
                return log.read_text().splitlines()

            calls = run()
            self.assertEqual((dest / "weather.key").read_text(), "key 1\n")
            self.assertEqual((dest / "weather.key").stat().st_mode & 0o777, 0o400)
            self.assertEqual(calls[-1], "restart abc123")
            self.assertIn("label=com.docker.compose.project=ix-wllharvest", calls[0])
            self.assertEqual(run(), [])  # unchanged: no copy, no restart
            (src / "w.crt").write_text("cert 2\n")  # renewed by TrueNAS
            self.assertEqual(run()[-1], "restart abc123")
            self.assertEqual((dest / "weather.crt").read_text(), "cert 2\n")
            self.assertEqual(sorted(p.name for p in dest.iterdir()), ["weather.crt", "weather.key"])

    def test_ensure_cron(self) -> None:
        none = FakeClient({"cronjob.query": []})
        self.assertEqual(dep.ensure_cron(none, "true"), "created")
        method, (data,), _ = none.calls[-1]
        self.assertEqual((method, data["user"], data["command"]), ("cronjob.create", "root", "true"))
        same = FakeClient({"cronjob.query": [{"id": 7, "command": "true", "enabled": True}]})
        self.assertEqual(dep.ensure_cron(same, "true"), "unchanged")
        self.assertEqual(same.methods(), ["cronjob.query"])
        old = FakeClient({"cronjob.query": [{"id": 7, "command": "false", "enabled": True}]})
        self.assertEqual(dep.ensure_cron(old, "true"), "updated")
        self.assertEqual(old.calls[-1][:2], ("cronjob.update", (7, old.calls[-1][1][1])))

    def test_restrict_sets_plain_owner_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            top = Path(d) / "code"
            (top / "web").mkdir(parents=True)
            (top / "web" / "a.js").write_text("x")
            dep.restrict(top, os.getuid(), os.getgid(), 0o750, 0o640)
            self.assertEqual(top.stat().st_mode & 0o777, 0o750)
            self.assertEqual((top / "web").stat().st_mode & 0o777, 0o750)
            self.assertEqual((top / "web" / "a.js").stat().st_mode & 0o777, 0o640)

    def test_web_port(self) -> None:
        self.assertEqual(dep.web_port(COMPOSE), "8580")
        self.assertIsNone(dep.web_port("services:\n  wllharvest:\n    image: x\n"))


if __name__ == "__main__":
    unittest.main()
