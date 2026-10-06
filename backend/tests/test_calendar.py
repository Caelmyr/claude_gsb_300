"""Unit tests for the calendar/shift engine and result-freshness layer.

Run with:  python -m unittest backend.tests.test_calendar -v
(no Flask required -- these exercise the pure-Python domain logic).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from .. import calendar as cal
from .. import freshness, models, seed, sensitivity, storage
from ..solvers import base as solver_base


def make_problem(horizon=72, spd=24, start="2024-01-01"):
    return models.Problem(
        id="t_cal", name="日历测试", horizon=horizon,
        time_unit="小时", start_date=start, slots_per_day=spd,
        resources=[
            models.Resource(id="W", name="工人", type="personnel"),
            models.Resource(id="M", name="机器", type="equipment"),
        ],
        shifts=[
            models.ShiftTemplate(id="day", name="白班", segments=[[8, 16]]),
            models.ShiftTemplate(id="swing", name="中班", segments=[[16, 24]]),
            models.ShiftTemplate(id="night", name="夜班", segments=[[0, 8]]),
        ],
        calendars=[
            models.CalendarTemplate(
                id="weekday", name="工作日",
                weekday_shifts={str(i): ["day"] for i in range(5)},
                holidays={"2024-01-01": "closed"} if start == "2024-01-01" else {}),
            models.CalendarTemplate(
                id="rot3", name="三班倒",
                weekday_shifts={str(i): ["day", "swing", "night"] for i in range(7)}),
        ],
        bindings=[
            models.CalendarBinding(id="bw", resource_id="W", calendar_id="weekday"),
            models.CalendarBinding(id="bm", resource_id="M", calendar_id="rot3"),
        ],
        tasks=[
            models.Task(id="T1", duration=2, resource_requirements={"W": 1, "M": 1}),
        ],
    )


class CalendarExpansionTests(unittest.TestCase):
    def test_weekday_only_with_holiday(self):
        p = make_problem(horizon=72)
        ivs = cal.expand_resource(p, "W")
        # Mon 2024-01-01 is a holiday (closed), Tue-Fri day shift 8-16.
        # day0 slots [0,24) closed; day1 [24,48): [32,40); day2 [48,72): [56,64)
        self.assertEqual(ivs, [[32, 40], [56, 64]])

    def test_rotating_covers_three_shifts_and_weekends(self):
        p = make_problem(horizon=72)
        ivs = cal.expand_resource(p, "M")
        # 0-8 + 8-16 + 16-24 merge into one contiguous block per day,
        # including Sat/Sun (days 5,6 beyond this horizon; days 0-2 covered).
        self.assertEqual(ivs, [[0, 72]])

    def test_night_shift_spill_and_clipping(self):
        p = models.Problem(
            id="t_night", horizon=26, slots_per_day=24, start_date="2024-01-01",
            resources=[models.Resource(id="N")],
            shifts=[models.ShiftTemplate(id="night", name="夜班", segments=[[22, 26]])],
            calendars=[models.CalendarTemplate(
                id="c", weekday_shifts={str(i): ["night"] for i in range(7)})],
            bindings=[models.CalendarBinding(id="b", resource_id="N", calendar_id="c")],
            tasks=[],
        )
        ivs = cal.expand_resource(p, "N")
        # Mon [22,24) clipped; Tue [24,26) is the spill of Mon's night shift
        # and also Tue's own [22,24) -> merged [22,26).
        self.assertEqual(ivs, [[22, 26]])

    def test_binding_date_range_and_priority(self):
        p = make_problem(horizon=120)
        # Worker W normally weekdays-only; temporary rotating stint Wed 01-03.
        p.bindings.append(models.CalendarBinding(
            id="temp", resource_id="W", calendar_id="rot3",
            start_date="2024-01-03", end_date="2024-01-03", priority=10))
        ivs = cal.expand_resource(p, "W")
        # day 2 (Wed) now fully covered [48,72); Tue [32,40), Fri [80,88),
        # weekend closed.
        self.assertIn([48, 72], ivs)
        self.assertIn([32, 40], ivs)
        self.assertIn([80, 88], ivs)
        # nothing on Sat (day5) / Sun (day6)
        self.assertFalse(cal.segments_intersect(ivs, 120, 144))
        self.assertFalse(cal.segments_intersect(ivs, 144, 168))

    def test_unbound_resource_has_no_generated_intervals(self):
        p = make_problem()
        p.resources.append(models.Resource(id="X"))
        self.assertEqual(cal.expand_resource(p, "X"), [])
        self.assertEqual(cal.preview_refresh(p, ["X"]), [])

    def test_refresh_generates_and_is_idempotent(self):
        p = make_problem()
        result = cal.refresh_all(p)
        self.assertEqual(result["n_changed"], 2)
        w = p.resource_map()["W"]
        self.assertEqual(w.availability_meta["source"], "calendar")
        self.assertTrue(w.availability_meta["signature"])
        # second run: nothing changed
        result2 = cal.refresh_all(p)
        self.assertEqual(result2["n_changed"], 0)
        self.assertEqual(result2["n_unchanged"], 2)

    def test_refresh_detects_template_edit_and_manual_overwrite(self):
        p = make_problem()
        cal.refresh_all(p)
        sig_before = p.resource_map()["W"].availability_meta["signature"]
        # edit the day shift 8-16 -> 9-17
        p.shift_map()["day"].segments = [[9, 17]]
        rows = {r["resource_id"]: r for r in cal.preview_refresh(p)}
        self.assertTrue(rows["W"]["changed"])
        self.assertNotEqual(rows["W"]["signature"], sig_before)
        cal.refresh_all(p)
        # Tue shift moves from [32,40) to [33,41)
        self.assertEqual(p.resource_map()["W"].availability,
                         [[33, 41], [57, 65]])

        # hand-edit, then refresh -> flagged as overwrite
        p.resources[0].availability = [[33, 38]]
        p.resources[0].availability_meta = {"source": "manual"}
        out = cal.refresh_all(p, ["W"])
        self.assertIn("W", out["overwritten_manual"])


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_root = storage.INSTANCES_ROOT
        storage.INSTANCES_ROOT = os.path.join(self.tmp, "instances")
        os.makedirs(storage.INSTANCES_ROOT, exist_ok=True)

    def tearDown(self):
        storage.INSTANCES_ROOT = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _persisted_problem(self):
        p = make_problem(horizon=120)
        cal.refresh_all(p)
        return storage.save_problem(p)

    def test_solution_is_current_then_stale_after_shift_change(self):
        p = self._persisted_problem()
        solver = solver_base.get_solver("greedy")
        sol = solver.solve(p, {})
        storage.save_solution(p.id, sol)
        self.assertIsNotNone(sol.input_fingerprint)

        summary = freshness.freshness_summary(p)
        self.assertEqual(summary["counts"]["solutions_stale"], 0)

        # change the day shift and refresh -> v2, existing solution goes stale
        p.shift_map()["day"].segments = [[6, 14]]
        cal.refresh_all(p)
        storage.save_problem(p)
        summary = freshness.freshness_summary(p)
        self.assertEqual(summary["counts"]["solutions_stale"], 1)
        row = summary["solutions"][0]
        self.assertEqual(row["freshness"], "stale")
        self.assertTrue(any("可用时段" in r for r in row["stale_reasons"]),
                        row["stale_reasons"])

    def test_unrelated_edit_does_not_blame_calendar(self):
        p = self._persisted_problem()
        solver = solver_base.get_solver("greedy")
        storage.save_solution(p.id, solver.solve(p, {}))
        p.tasks[0].duration = 5
        storage.save_problem(p)
        row = freshness.freshness_summary(p)["solutions"][0]
        self.assertTrue(row["stale"])
        self.assertTrue(any("工期" in r for r in row["stale_reasons"]))
        self.assertFalse(any("可用时段" in r for r in row["stale_reasons"]))

    def test_sensitivity_and_report_inherit_staleness(self):
        p = self._persisted_problem()
        result = sensitivity.run_sensitivity(
            p, "greedy", {"kind": "task_duration", "task": "T1",
                          "offsets": [0, 1]}, persist=True)
        solver = solver.get_solver("greedy") if False else solver_base.get_solver("greedy")
        sol = solver.solve(p, {})
        storage.save_solution(p.id, sol)
        from .. import report as rep_mod
        rep = rep_mod.generate_report(p, [sol], result, persist=True)

        # all current to begin with
        summary = freshness.freshness_summary(p)
        self.assertEqual(summary["counts"]["stale_total"], 0)

        # shift change invalidates every artefact, transitively the report
        p.shift_map()["day"].segments = [[10, 18]]
        cal.refresh_all(p)
        storage.save_problem(p)
        summary = freshness.freshness_summary(p)
        counts = summary["counts"]
        self.assertEqual(counts["solutions_stale"], 1)
        self.assertEqual(counts["sensitivity_stale"], 1)
        self.assertEqual(counts["reports_stale"], 1)
        rep_row = next(r for r in summary["reports"] if r["id"] == rep.id)
        self.assertTrue(any("方案" in r for r in rep_row["stale_reasons"]))
        self.assertTrue(any("敏感性" in r for r in rep_row["stale_reasons"]))

    def test_legacy_artefact_without_fingerprint_is_unverified(self):
        p = self._persisted_problem()
        legacy = models.Solution(id="sol_old", problem_id=p.id, solver="greedy")
        storage.save_solution(p.id, legacy)
        row = freshness.freshness_summary(p)["solutions"][0]
        self.assertEqual(row["freshness"], "unverified")
        self.assertTrue(row["stale"])

    def test_resolve_after_resolve_clears_flag(self):
        p = self._persisted_problem()
        solver = solver_base.get_solver("greedy")
        storage.save_solution(p.id, solver.solve(p, {}))
        p.shift_map()["day"].segments = [[7, 15]]
        cal.refresh_all(p)
        storage.save_problem(p)
        self.assertEqual(freshness.freshness_summary(p)["counts"]["solutions_stale"], 1)
        # re-solve on the new input -> current again
        storage.save_solution(p.id, solver.solve(p, {}))
        summary = freshness.freshness_summary(p)
        # old one stays stale, new one is current
        self.assertEqual(summary["counts"]["solutions_stale"], 1)
        self.assertEqual(summary["counts"]["solutions_total"], 2)
        fresh = [s for s in summary["solutions"] if not s["stale"]]
        self.assertEqual(len(fresh), 1)


class ValidationTests(unittest.TestCase):
    def test_dangling_binding_and_bad_shift_rejected(self):
        p = make_problem()
        p.bindings.append(models.CalendarBinding(
            id="bad", resource_id="GHOST", calendar_id="weekday"))
        errors = models.validate_problem(p)
        self.assertTrue(any("GHOST" in e for e in errors))

        p2 = make_problem()
        p2.calendars[0].weekday_shifts["0"] = ["nope"]
        errors = models.validate_problem(p2)
        self.assertTrue(any("nope" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
