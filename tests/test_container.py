"""Docker isolation: the argv is a pure function (always tested); the integration tests need a running Docker
(the pinned image is pulled on first use) and are skipped otherwise."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import test_invariants as ti  # noqa: E402
import test_adversarial as ta  # noqa: E402
from observatory import adapter, catalog, cli, runner  # noqa: E402
from observatory.model import AdapterRef, Mode, Status  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures"
IMAGE = "python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"


def docker_ready() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Argv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.script = str(Path(self._tmp.name, "a.py"))
        Path(self.script).write_text("")
        self.ref = AdapterRef(("python3", self.script, "--flag"), "v", image=IMAGE)

    def argv(self, ref=None, env=None):
        return adapter.docker_argv(ref or self.ref, adapter.Verb.RUN_TASK, name="obs-x", cwd="/c/call", scratch="/s/run",
                                   env=env or {"PATH": "/host/bin", "HOME": "/c/call", "OBS_STATE_DIR": "/s/run/state"},
                                   memory_mb=512)

    def test_exactly_the_declared_mounts(self):
        a = self.argv()
        self.assertEqual([a[i + 1] for i, x in enumerate(a) if x == "-v"],
                         ["/c/call:/c/call", "/s/run:/s/run", f"{self.script}:{self.script}:ro"])

    def test_no_network_unless_hosts_declared(self):
        a = self.argv()
        self.assertEqual(a[a.index("--network") + 1], "none")
        b = self.argv(replace(self.ref, network_hosts=("api.example.com",)))
        self.assertEqual(b[b.index("--network") + 1], "bridge")

    def test_limits_and_command_tail(self):
        a = self.argv()
        self.assertEqual(a[a.index("--memory") + 1], "512m")
        self.assertEqual(a[a.index("--pids-limit") + 1], str(adapter.PIDS_LIMIT))
        self.assertEqual(a[-5:], [IMAGE, "python3", self.script, "--flag", "run-task"])
        self.assertEqual(a[:5], ["docker", "run", "--rm", "-i", "--name"])

    def test_secret_values_stay_out_of_argv_and_host_path_is_not_forwarded(self):
        a = self.argv(env={"PATH": "/host/bin", "HOME": "/c/call", "OBS_STATE_DIR": "/s", "API_KEY": "hunter2"})
        self.assertNotIn("hunter2", " ".join(a))
        self.assertIn("API_KEY", a)
        self.assertIn("HOME=/c/call", a)
        self.assertFalse(any(x.startswith("PATH") for x in a))

    def test_isolation_records_what_is_enforced(self):
        d = dict(adapter.isolation(ti.BUDGET, self.ref))
        self.assertEqual((d["filesystem"], d["network"]), ("container", "none"))
        d = dict(adapter.isolation(ti.BUDGET, replace(self.ref, network_hosts=("a.com", "b.com"))))
        self.assertEqual(d["network"], "declared_hosts_not_enforced:a.com,b.com")
        d = dict(adapter.isolation(ti.BUDGET, replace(self.ref, image="")))
        self.assertEqual((d["filesystem"], d["network"]), ("not_enforced", "not_enforced"))


class CatalogKeys(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "fx"
        shutil.copytree(FIXTURE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        self.cfg = self.root / "catalog" / "configs" / "baseline-m1.toml"
        self.original = self.cfg.read_text()

    def load_with(self, extra: str):
        self.cfg.write_text(extra + "\n" + self.original)   # top-level keys must precede the [settings] table
        return catalog.load(self.root / "catalog")

    def test_keys_parse_into_identity_and_change_the_hash(self):
        base = catalog.load(self.root / "catalog").configs["baseline-m1"]
        c = self.load_with(f'container_image = "{IMAGE}"\nnetwork_hosts = ["b.com", "a.com"]\nsecret_env = ["K"]')
        c = c.configs["baseline-m1"]
        self.assertEqual((c.adapter.image, c.adapter.network_hosts, c.adapter.secret_env), (IMAGE, ("a.com", "b.com"), ("K",)))
        self.assertNotEqual(c.identity_hash, base.identity_hash)

    def test_unpinned_image_is_a_catalog_problem(self):
        for bad in ("python:3.12-slim", "python@sha256:abc"):
            with self.assertRaises(catalog.CatalogError) as cm:
                self.load_with(f'container_image = "{bad}"')
            self.assertIn("pinned by digest", str(cm.exception))

    def test_bad_secret_name_is_a_catalog_problem(self):
        with self.assertRaises(catalog.CatalogError):
            self.load_with('secret_env = ["A=B"]')


@unittest.skipUnless(docker_ready(), "docker is unavailable")
class Container(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.script = self.root / "adapters" / "probe.py"
        self.script.parent.mkdir()
        shutil.copy(FIXTURE / "adapters" / "probe.py", self.script)
        self.ref_file = self.root / "evaluator" / "ref.json"   # outside every mount: stands in for a package reference
        self.ref_file.parent.mkdir()
        self.ref_file.write_text('{"answer": 42}')

    def invoke(self, settings: dict, *, image: str = IMAGE, hosts: tuple[str, ...] = (), secrets=None, timeout=60, script=None):
        ref = AdapterRef(("python3", str(script or self.script)), "x", image=image, network_hosts=hosts)
        return adapter.invoke(ref, adapter.Verb.RUN_TASK, {"settings": settings}, artifact_dir=self.root / "a",
                              state_dir=self.root / "run" / "state", timeout_s=timeout, budget=ti.BUDGET,
                              secrets=secrets or {})

    def test_candidate_cannot_read_outside_its_mounts(self):
        host = self.invoke({"read": str(self.ref_file)}, image="")
        self.assertEqual(host.output["read"], '{"answer": 42}', "control: a host process reads it")
        boxed = self.invoke({"read": str(self.ref_file)})
        self.assertIsNone(boxed.failure, boxed.error)
        self.assertEqual(boxed.output["read_error"], "FileNotFoundError")
        self.assertNotIn("read", boxed.output)
        self.assertTrue(boxed.output["state_write"])

    def test_no_network_unless_declared(self):
        r = self.invoke({"connect": "1.1.1.1:80"})
        self.assertIsNone(r.failure, r.error)
        self.assertIn("connect_error", r.output)
        self.assertNotIn("connect", r.output)

    def test_secret_reaches_the_adapter_by_name(self):
        r = self.invoke({}, secrets={"OBS_TEST_SECRET": "s3cret"})
        self.assertIn("OBS_TEST_SECRET", r.output["env"])

    def test_timeout_kills_the_container(self):
        sleeper = self.root / "adapters" / "sleep.py"
        sleeper.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(120)\n")
        r = self.invoke({}, timeout=3, script=sleeper)
        self.assertIs(r.failure, Status.TIMEOUT)
        live = subprocess.run(["docker", "ps", "-q", "--filter", "name=obs-"], capture_output=True, text=True).stdout
        self.assertEqual(live.strip(), "")

    def test_docker_failing_to_start_is_not_a_candidate_outcome(self):
        with self.assertRaises(adapter.AdapterNotStartable):
            self.invoke({}, image="obs-no-such-image@sha256:" + "0" * 64)


@unittest.skipUnless(docker_ready(), "docker is unavailable")
class ThroughTheRunner(ta.Fx):
    """A containerized config runs plan -> execute -> `obs run` with its declared secret."""

    def setUp(self):
        super().setUp()
        self.config("boxed")
        f = self.root / "catalog" / "configs" / "boxed.toml"
        f.write_text(f'container_image = "{IMAGE}"\nsecret_env = ["OBS_TEST_KEY"]\n' + f.read_text())

    def test_obs_run_passes_the_declared_secret_and_records_isolation(self):
        cat, st = self.load()
        p = runner.plan(st, cat, config_id="boxed", package=ta.PKG, mode=Mode.CONTROLLED)
        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps({"config_id": "boxed", "package": ta.PKG, "mode": "controlled", "track": p.track.value,
                                         "purpose": p.purpose.value, "repetitions": p.repetitions, "plan_id": p.plan_id,
                                         "evaluator": "jay"}))
        argv = ["--catalog", str(self.root / "catalog"), "--store", str(self.root / "store"), "run", str(plan_file)]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OBS_TEST_KEY", None)
            self.assertEqual(cli.main(argv), 1)
        self.assertIn("OBS_TEST_KEY", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), unittest.mock.patch.dict(os.environ, {"OBS_TEST_KEY": "v"}):
            self.assertEqual(cli.main(argv), 0)
        rec = st.run(p.plan_id)
        self.assertEqual(dict(rec.manifest.isolation)["filesystem"], "container")
        self.assertTrue(all(a.failure is None for a in rec.attempts), [a.error for a in rec.attempts])


if __name__ == "__main__":
    unittest.main()
