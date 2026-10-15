"""Progress-based deadline: a scan node that keeps finishing files is never stopped by its deadline."""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))
import watchdog_instance as inst_wd  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
BUSY = (90.0, 1)  # GPU 90%, one GPU process


def cfg() -> inst_wd.Config:
    return inst_wd.Config(instance_id="i-1", deadline=T0 + dt.timedelta(hours=8),
                          progress_glob="x/*.done.json", stall_minutes=60)


class ProgressDeadline(unittest.TestCase):
    def run_checks(self, steps: list[tuple[float, int]]) -> inst_wd.Decision:
        """steps: (hours after T0, done-file count); returns the last decision."""
        c, state, d = cfg(), {}, None
        for h, n in steps:
            d = inst_wd.decide(c, state, T0 + dt.timedelta(hours=h), BUSY, ["scan"], None, n)
        assert d is not None
        return d

    def test_past_deadline_but_finishing_files_keeps_running(self) -> None:
        d = self.run_checks([(0, 0), (7.5, 1000), (8.5, 1100), (9.0, 1200)])
        self.assertFalse(d.terminate, d.reason)
        self.assertIn("moved out by progress", d.reason)

    def test_stuck_after_deadline_terminates_an_hour_after_last_file(self) -> None:
        self.assertFalse(self.run_checks([(0, 0), (8.5, 500), (9.4, 500)]).terminate)
        d = self.run_checks([(0, 0), (8.5, 500), (9.6, 500)])
        self.assertTrue(d.terminate)
        self.assertIn("no finished files", d.reason)

    def test_stuck_before_deadline_still_stops_at_launch_deadline(self) -> None:
        d = self.run_checks([(0, 0), (1, 10), (8.1, 10)])
        self.assertTrue(d.terminate)
        self.assertIn("max lifetime", d.reason)

    def test_without_progress_glob_old_behaviour(self) -> None:
        c = cfg()
        c.progress_glob = ""
        d = inst_wd.decide(c, {}, T0 + dt.timedelta(hours=8.1), BUSY, ["scan"], None, None)
        self.assertTrue(d.terminate)


if __name__ == "__main__":
    unittest.main()
