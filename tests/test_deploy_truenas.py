"""Tests for deploy_truenas.py's decisions, against a fake TrueNAS API client.

These run on the NAS's own Python (3.11) as well as 3.13+, and need PyYAML,
which TrueNAS ships:  python3 -m unittest discover -s tests -p 'test_deploy*'
"""

import json
import os
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
        self.assertEqual(set(services), {"wllharvest", "web"})
        for svc in services.values():
            self.assertEqual(svc["volumes"], [f"{MNT}/code:/app:ro", f"{MNT}/data:/data"])
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
            src, code = Path(d) / "wllharvest.py", Path(d) / "code"
            code.mkdir()
            src.write_text("print('v1')\n")
            self.assertTrue(dep.install_code(src, code))
            target = code / "wllharvest.py"
            self.assertEqual(target.read_text(), "print('v1')\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertFalse(dep.install_code(src, code))
            src.write_text("print('v2')\n")
            self.assertTrue(dep.install_code(src, code))
            self.assertEqual(sorted(os.listdir(code)), ["wllharvest.py"])

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

    def test_web_port(self) -> None:
        self.assertEqual(dep.web_port(COMPOSE), "8580")
        self.assertIsNone(dep.web_port("services:\n  wllharvest:\n    image: x\n"))


if __name__ == "__main__":
    unittest.main()
