"""Offline tests of both watchdogs against a fake Lambda Cloud API (no network, no real key, no instance).

    python -m unittest discover -s tests -v          # from pipeline/
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))
import watchdog as local_wd  # noqa: E402
import watchdog_instance as inst_wd  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class FakeLambda:
    """Minimal Lambda Cloud API: GET /instances, POST /instance-operations/terminate."""

    def __init__(self, instances: list[dict[str, Any]]) -> None:
        self.instances = instances
        self.terminate_codes: list[int] = []  # popped per terminate call; 200 when empty
        self.calls: list[tuple[str, str, str, Any]] = []  # (method, path, auth header, body)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, obj: Any) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                fake.calls.append(("GET", self.path, self.headers.get("Authorization", ""), None))
                if self.path.endswith("/instances"):
                    self._send(200, {"data": fake.instances})
                else:
                    self._send(404, {"error": {"code": "not-found"}})

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.calls.append(("POST", self.path, self.headers.get("Authorization", ""), body))
                code = fake.terminate_codes.pop(0) if fake.terminate_codes else 200
                if code != 200:
                    self._send(code, {"error": {"code": f"http-{code}"}})
                    return
                ids = body.get("instance_ids", [])
                known = {i["id"] for i in fake.instances}
                if not set(ids) <= known:
                    self._send(404, {"error": {"code": "global/object-does-not-exist"}})
                    return
                for i in fake.instances:
                    if i["id"] in ids:
                        i["status"] = "terminating"
                self._send(200, {"data": {"terminated_instances": [{"id": x, "status": "terminating"} for x in ids]}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def terminated(self) -> list[str]:
        return [x for m, p, _, b in self.calls if m == "POST" for x in b["instance_ids"]]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def inst(iid: str, name: str, status: str = "active", ip: str = "10.0.0.1") -> dict[str, Any]:
    return {"id": iid, "name": name, "status": status, "ip": ip}


class LocalWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.key = d / "key"
        self.key.write_text("key-1\n", encoding="utf-8")
        self.last_run = d / "last_run.json"
        self.state = d / "state.json"
        self.last_run.write_text(json.dumps({"instances": []}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def api(self, fake: FakeLambda) -> local_wd.Api:
        return local_wd.Api(fake.url, self.key, None, timeout=5)

    def test_name_deadline(self) -> None:
        self.assertEqual(local_wd.name_deadline("hh-scan2-x2610151830"), dt.datetime(2026, 10, 15, 18, 30, tzinfo=UTC))
        self.assertIsNone(local_wd.name_deadline("hh-main"))
        self.assertIsNone(local_wd.name_deadline("hh-main-x2613991830"))  # month 13: invalid

    def test_terminates_only_expired_hoophunter_instances(self) -> None:
        self.state.write_text(json.dumps({"first_seen": {"c": (T0 - dt.timedelta(hours=7)).isoformat()}}))
        fake = FakeLambda([
            inst("a", "hh-main-x2610151100"),           # deadline 11:00 < now 12:00
            inst("b", "hh-scan2-x2610151300"),          # 13:00 > now
            inst("c", "hoophunter-train"),              # no deadline, first seen 7 h ago > default 6 h
            inst("d", "someone-elses-box"),             # not ours: never touched
            inst("e", "hh-old-x2610140000", "terminated"),
        ])
        try:
            rc = local_wd.run_once(self.api(fake), last_run=self.last_run, state_file=self.state, now=T0, retry_s=0,
                                   sleep=lambda s: None)
        finally:
            fake.close()
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(fake.terminated()), ["a", "c"])

    def test_record_deadline_and_hard_cap(self) -> None:
        self.last_run.write_text(json.dumps({"instances": [
            {"name": "main", "instance_id": "r1", "launched_at": (T0 - dt.timedelta(hours=3)).isoformat(),
             "max_hours": 2, "status": "active"}]}), encoding="utf-8")
        fake = FakeLambda([
            inst("r1", "hh-main-x2610160000"),  # name says tomorrow, the record says 2 h after launch (11:00)
            inst("cap", "hh-big-x2612010000"),  # name says December; hard cap = first seen + 24 h
        ])
        self.state.write_text(json.dumps({"first_seen": {"cap": (T0 - dt.timedelta(hours=25)).isoformat()}}))
        try:
            local_wd.run_once(self.api(fake), last_run=self.last_run, state_file=self.state, now=T0, retry_s=0,
                              sleep=lambda s: None)
        finally:
            fake.close()
        self.assertEqual(sorted(fake.terminated()), ["cap", "r1"])
        rec = json.loads(self.last_run.read_text())["instances"][0]
        self.assertEqual(rec["status"], "terminated")
        self.assertIn("cloud/watchdog.py", rec["note"])

    def test_dry_run_never_posts(self) -> None:
        fake = FakeLambda([inst("a", "hh-main-x2610151100")])
        try:
            rc = local_wd.run_once(self.api(fake), last_run=self.last_run, state_file=self.state, now=T0, dry_run=True)
        finally:
            fake.close()
        self.assertEqual(rc, 0)
        self.assertEqual(fake.terminated(), [])

    def test_key_rotation_is_picked_up_between_retries(self) -> None:
        fake = FakeLambda([inst("a", "hh-main-x2610151100")])
        fake.terminate_codes = [401, 403]

        def rotate(_: float) -> None:  # the owner saves a new key while the watchdog retries
            self.key.write_text("key-2", encoding="utf-8")

        try:
            rc = local_wd.run_once(self.api(fake), last_run=self.last_run, state_file=self.state, now=T0, retry_s=60,
                                   sleep=rotate)
        finally:
            fake.close()
        self.assertEqual(rc, 0)
        posts = [c for c in fake.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[0][2], "Bearer key-1")
        self.assertEqual(posts[-1][2], "Bearer key-2")

    def test_persistent_failure_says_terminate_manually(self) -> None:
        fake = FakeLambda([inst("a", "hh-main-x2610151100")])
        fake.terminate_codes = [500] * 10
        ticks = iter(range(0, 10_000, 100))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                ok = local_wd.terminate(self.api(fake), "a", retry_s=250, sleep=lambda s: None,
                                        clock=lambda: float(next(ticks)))
        finally:
            fake.close()
        self.assertFalse(ok)
        fake2 = FakeLambda([inst("a", "hh-main-x2610151100")])
        fake2.terminate_codes = [500] * 10
        try:
            with redirect_stdout(buf):
                rc = local_wd.run_once(self.api(fake2), last_run=self.last_run, state_file=self.state, now=T0, retry_s=0,
                                       sleep=lambda s: None)
        finally:
            fake2.close()
        self.assertEqual(rc, 2)
        self.assertIn("TERMINATE MANUALLY: instance a", buf.getvalue())


class InstanceWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "key").write_text("key-1", encoding="utf-8")
        self.d = d

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cfg(self, fake: FakeLambda, iid: str = "me", hours: float = 6.0) -> inst_wd.Config:
        return inst_wd.Config(instance_id=iid, deadline=T0 + dt.timedelta(hours=hours), ip="10.0.0.1", name="hh-main",
                              api=fake.url, key_file=self.d / "key", state_file=self.d / "state.json",
                              log_file=self.d / "wd.log", hold_file=self.d / "hold", backoff_s=[0.0], attempts=3,
                              http_timeout_s=5)

    def check(self, cfg: inst_wd.Config, minutes: float, gpu: tuple[float, int] | None = (0.0, 0),
              jobs: list[str] | None = None, dry_run: bool = False) -> inst_wd.Decision:
        return inst_wd.run_check(cfg, dry_run=dry_run, now=T0 + dt.timedelta(minutes=minutes), gpu=lambda: gpu,
                                 jobs=lambda pats: list(jobs or []), sleep=lambda s: None, ips=lambda: ["10.0.0.1"])

    def test_max_lifetime(self) -> None:
        fake = FakeLambda([inst("me", "hh-main")])
        try:
            cfg = self.cfg(fake, hours=1)
            self.assertFalse(self.check(cfg, 59, gpu=(90.0, 1)).terminate)
            d = self.check(cfg, 61, gpu=(90.0, 1))  # busy GPU does not matter past the deadline
        finally:
            fake.close()
        self.assertTrue(d.terminate)
        self.assertIn("max lifetime", d.reason)
        self.assertEqual(fake.terminated(), ["me"])

    def test_idle_30_minutes(self) -> None:
        fake = FakeLambda([inst("me", "hh-main")])
        try:
            cfg = self.cfg(fake)
            self.assertFalse(self.check(cfg, 0).terminate)                    # first run starts the idle clock
            self.assertFalse(self.check(cfg, 20, gpu=(55.0, 1)).terminate)    # GPU busy at +20
            self.assertFalse(self.check(cfg, 45).terminate)                   # idle 25 min
            self.assertFalse(self.check(cfg, 60, jobs=["bash cloud/run_remote.sh job x"]).terminate)  # CPU job
            self.assertFalse(self.check(cfg, 89).terminate)                   # idle 29 min
            self.assertEqual(fake.terminated(), [])
            d = self.check(cfg, 91)                                           # idle 31 min
        finally:
            fake.close()
        self.assertTrue(d.terminate)
        self.assertIn("idle", d.reason)
        self.assertEqual(fake.terminated(), ["me"])

    def test_nvidia_smi_missing_still_times_out(self) -> None:
        fake = FakeLambda([inst("me", "hh-main")])
        try:
            cfg = self.cfg(fake)
            self.check(cfg, 0, gpu=None)
            d = self.check(cfg, 31, gpu=None)
        finally:
            fake.close()
        self.assertTrue(d.terminate)

    def test_hold_never_passes_the_deadline(self) -> None:
        fake = FakeLambda([inst("me", "hh-main")])
        try:
            cfg = self.cfg(fake, hours=2)
            self.check(cfg, 0)
            until = inst_wd.set_hold(cfg, 600, now=T0)  # asks for 10 h
            self.assertEqual(until, cfg.deadline)
            self.assertFalse(self.check(cfg, 100).terminate)   # held although idle for 100 min
            d = self.check(cfg, 121)                            # past the 2 h deadline
        finally:
            fake.close()
        self.assertTrue(d.terminate)
        self.assertEqual(fake.terminated(), ["me"])

    def test_dry_run(self) -> None:
        fake = FakeLambda([inst("me", "hh-main")])
        try:
            cfg = self.cfg(fake, hours=1)
            d = self.check(cfg, 90, dry_run=True)
        finally:
            fake.close()
        self.assertTrue(d.terminate)
        self.assertEqual(fake.calls, [])

    def test_id_from_ip_when_unknown_and_retry_after_401(self) -> None:
        fake = FakeLambda([inst("other", "hh-x", ip="10.9.9.9"), inst("real-id", "hh-main", ip="10.0.0.1")])
        fake.terminate_codes = [404, 401]  # stale id in the config, then a rotated key
        try:
            cfg = self.cfg(fake, iid="stale-id", hours=1)

            def rotate(_: float) -> None:
                (self.d / "key").write_text("key-2", encoding="utf-8")

            ok = inst_wd.terminate_self(cfg, sleep=rotate, ips=lambda: ["10.0.0.1"])
        finally:
            fake.close()
        self.assertTrue(ok)
        posts = [c for c in fake.calls if c[0] == "POST"]
        self.assertEqual([p[3]["instance_ids"] for p in posts], [["stale-id"], ["real-id"], ["real-id"]])
        self.assertEqual(posts[-1][2], "Bearer key-2")
        self.assertIn("real-id", fake.terminated())

    def test_gives_up_after_attempts_and_logs(self) -> None:
        fake = FakeLambda([inst("me", "hh-main")])
        fake.terminate_codes = [500] * 5
        try:
            cfg = self.cfg(fake, hours=1)
            inst_wd.setup_logging(cfg.log_file)
            ok = inst_wd.terminate_self(cfg, sleep=lambda s: None)
        finally:
            fake.close()
            root = logging.getLogger()
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)
        self.assertFalse(ok)
        self.assertEqual(len([c for c in fake.calls if c[0] == "POST"]), cfg.attempts)
        text = (self.d / "wd.log").read_text(encoding="utf-8")
        self.assertIn("TERMINATE FAILED", text)
        self.assertNotIn("key-1", text)  # the key never reaches the log

    def test_config_file_roundtrip(self) -> None:
        p = self.d / "watchdog.json"
        p.write_text(json.dumps({"instance_id": "abc", "ip": "1.2.3.4", "name": "hh-main-x2610151800",
                                 "deadline": "2026-10-15T18:00:00+00:00", "idle_minutes": 45, "gpu_idle_pct": 5,
                                 "api": "https://cloud.lambda.ai/api/v1"}), encoding="utf-8")
        cfg = inst_wd.Config.load(p)
        self.assertEqual(cfg.instance_id, "abc")
        self.assertEqual(cfg.idle_minutes, 45.0)
        self.assertEqual(cfg.deadline, dt.datetime(2026, 10, 15, 18, 0, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
