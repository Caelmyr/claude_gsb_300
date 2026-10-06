"""
Shift / calendar templates and staleness tracking.

This module turns :class:`backend.models.CalendarTemplate` patterns (shifts,
workdays, holidays) into concrete resource ``availability`` intervals, applies
them to problem instances, and — critically — tracks which derived artefacts
(solutions, sensitivity runs, reports) were produced against which calendar
state, so that a template change visibly invalidates old results instead of
silently leaving a stale schedule to be executed.

Versioning model
----------------
* Every template save bumps ``CalendarTemplate.version``.
* Every calendar apply/refresh on a problem bumps that problem's
  ``calendar_version`` (stored in ``calendar_state.json`` next to the
  instance) and rewrites the affected resources' ``availability`` (which in
  turn bumps ``problem.version`` via the normal save path).
* Solutions, sensitivity results and reports are stamped with the
  ``problem_version`` / ``calendar_version`` in effect when they were
  created.  :func:`staleness` compares the stamps against the current
  counters and explains, per artefact, why it is outdated.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Dict, List, Optional, Tuple

from . import models, storage

# --------------------------------------------------------------------------- #
# Built-in presets
# --------------------------------------------------------------------------- #

def preset_templates() -> List[Dict[str, Any]]:
    """Factory presets for common shift systems.  ``origin_date`` defaults to
    a Monday so weekday labels line up; users can adjust after creation."""
    origin = "2026-01-05"  # a Monday
    return [
        {
            "key": "three_shift",
            "name": "三班倒（8h×3，全年无休）",
            "description": "早班 8-16 / 中班 16-24 / 夜班 0-8，一周七天连续运转。",
            "origin_date": origin,
            "slots_per_day": 24,
            "shifts": [
                {"name": "夜班", "start": 0, "end": 8},
                {"name": "早班", "start": 8, "end": 16},
                {"name": "中班", "start": 16, "end": 24},
            ],
            "workdays": [0, 1, 2, 3, 4, 5, 6],
            "holidays": [],
        },
        {
            "key": "two_shift",
            "name": "两班倒（12h×2，工作日）",
            "description": "白班 8-20 / 夜班 20-32（跨天），周一至周五。",
            "origin_date": origin,
            "slots_per_day": 24,
            "shifts": [
                {"name": "白班", "start": 8, "end": 20},
                {"name": "夜班", "start": 20, "end": 32},
            ],
            "workdays": [0, 1, 2, 3, 4],
            "holidays": [],
        },
        {
            "key": "day_only",
            "name": "白班（8h，周末双休）",
            "description": "9-17 单班，周一至周五。",
            "origin_date": origin,
            "slots_per_day": 24,
            "shifts": [{"name": "白班", "start": 9, "end": 17}],
            "workdays": [0, 1, 2, 3, 4],
            "holidays": [],
        },
        {
            "key": "maintenance",
            "name": "设备运行（6 天 + 周日保养）",
            "description": "设备 0-24 连续可用，周日停机保养。",
            "origin_date": origin,
            "slots_per_day": 24,
            "shifts": [{"name": "运行", "start": 0, "end": 24}],
            "workdays": [0, 1, 2, 3, 4, 5],
            "holidays": [],
        },
    ]


# --------------------------------------------------------------------------- #
# Availability generation
# --------------------------------------------------------------------------- #

def _parse_date(s: str) -> _dt.date:
    return _dt.date.fromisoformat(s)


def _holiday_dates(template: models.CalendarTemplate) -> set:
    """Expand inclusive [start, end] holiday ranges into a set of ISO dates."""
    out = set()
    for rng in template.holidays:
        try:
            a, b = _parse_date(rng[0]), _parse_date(rng[1])
        except (ValueError, IndexError, TypeError):
            continue
        if b < a:
            a, b = b, a
        d = a
        while d <= b:
            out.add(d.isoformat())
            d += _dt.timedelta(days=1)
    return out


def _as_slot(x: float) -> Any:
    """Collapse integral floats to int so intervals stay JSON-clean."""
    return int(x) if float(x).is_integer() else round(float(x), 4)


def merge_intervals(intervals: List[List[float]]) -> List[List[Any]]:
    """Sort and merge overlapping/adjacent [start, end) intervals."""
    iv = sorted((float(a), float(b)) for a, b in intervals if b > a)
    merged: List[List[float]] = []
    for a, b in iv:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [[_as_slot(a), _as_slot(b)] for a, b in merged]


def generate_availability(template: models.CalendarTemplate,
                          horizon: int,
                          start_date: Optional[str] = None,
                          end_date: Optional[str] = None) -> List[List[Any]]:
    """Expand a template into merged ``[start, end)`` slot intervals covering
    slots ``[0, horizon)``, optionally restricted to a calendar date range.

    A shift window ``[s, e)`` on day ``d`` becomes
    ``[d*slots_per_day + s, d*slots_per_day + e)``; ``e`` may exceed
    ``slots_per_day`` so overnight shifts (e.g. 夜班 22→30) naturally extend
    into the next day's slots and are attributed to the day they start on
    (standard for night-shift scheduling and holiday handling).
    """
    if horizon <= 0 or not template.shifts:
        return []
    spd = template.slots_per_day
    origin = _parse_date(template.origin_date)
    n_days = math.ceil(horizon / spd)

    first_day = 0
    last_day = n_days - 1
    if start_date:
        first_day = max(first_day, (_parse_date(start_date) - origin).days)
    if end_date:
        last_day = min(last_day, (_parse_date(end_date) - origin).days)
    if last_day < first_day:
        return []

    holidays = _holiday_dates(template)
    workdays = set(template.workdays)

    intervals: List[List[float]] = []
    for d in range(first_day, last_day + 1):
        date = origin + _dt.timedelta(days=d)
        if date.weekday() not in workdays:
            continue
        if date.isoformat() in holidays:
            continue
        base = d * spd
        for sh in template.shifts:
            s = max(0.0, min(float(horizon), base + float(sh["start"])))
            e = max(0.0, min(float(horizon), base + float(sh["end"])))
            if e > s:
                intervals.append([s, e])
    return merge_intervals(intervals)


# --------------------------------------------------------------------------- #
# Apply / refresh
# --------------------------------------------------------------------------- #

def apply_template(problem: models.Problem,
                   template: models.CalendarTemplate,
                   resource_ids: List[str],
                   start_date: Optional[str] = None,
                   end_date: Optional[str] = None) -> Tuple[models.CalendarAssignment, List[str]]:
    """Apply a template to the given resources of ``problem``: regenerate
    their ``availability``, persist the problem (bumping its version) and
    record the assignment while bumping the problem's ``calendar_version``.

    Returns the recorded assignment and the list of resource ids actually
    updated (unknown ids are skipped).
    """
    availability = generate_availability(template, problem.horizon,
                                         start_date, end_date)
    by_id = problem.resource_map()
    updated: List[str] = []
    for rid in resource_ids:
        res = by_id.get(rid)
        if res is None:
            continue
        res.availability = [list(iv) for iv in availability]
        updated.append(rid)
    if not updated:
        raise ValueError("没有匹配的资源可套用")

    storage.save_problem(problem)

    assignment = models.CalendarAssignment(
        id=models.new_id("cal"),
        template_id=template.id,
        resource_ids=updated,
        start_date=start_date,
        end_date=end_date,
        template_version=template.version,
    )
    state = storage.load_calendar_state(problem.id)
    state["version"] = int(state.get("version", 0)) + 1
    state["assignments"].append(assignment.to_dict())
    storage.save_calendar_state(problem.id, state)
    return assignment, updated


def refresh_template(template_id: str) -> List[Dict[str, Any]]:
    """Batch-refresh every assignment that references ``template_id``, across
    all problem instances.  Each touched problem gets regenerated
    availability, a bumped ``calendar_version`` and a saved problem version,
    which is exactly what :func:`staleness` keys on to flag outdated results.
    """
    template = storage.load_calendar_template(template_id)
    if template is None:
        raise ValueError(f"unknown calendar template: {template_id}")

    summary: List[Dict[str, Any]] = []
    for meta in storage.list_problems():
        pid = meta["id"]
        state = storage.load_calendar_state(pid)
        relevant = [a for a in state["assignments"]
                    if a.get("template_id") == template_id]
        if not relevant:
            continue
        problem = storage.load_problem(pid)
        if problem is None:
            continue
        by_id = problem.resource_map()
        resources_touched: List[str] = []
        for a in relevant:
            availability = generate_availability(
                template, problem.horizon, a.get("start_date"), a.get("end_date"))
            for rid in a.get("resource_ids", []):
                res = by_id.get(rid)
                if res is None:
                    continue
                res.availability = [list(iv) for iv in availability]
                if rid not in resources_touched:
                    resources_touched.append(rid)
            a["template_version"] = template.version
            a["applied_at"] = models.now_iso()
        state["version"] = int(state.get("version", 0)) + 1
        storage.save_calendar_state(pid, state)
        storage.save_problem(problem)
        stale = staleness(problem)
        summary.append({
            "problem_id": pid,
            "problem_name": meta.get("name", pid),
            "assignments_refreshed": len(relevant),
            "resources": resources_touched,
            "calendar_version": state["version"],
            "stale_solutions": sum(1 for s in stale["solutions"] if s["stale"]),
            "stale_sensitivity": sum(1 for s in stale["sensitivity"] if s["stale"]),
            "stale_reports": sum(1 for s in stale["reports"] if s["stale"]),
        })
    return summary


def remove_assignment(problem: models.Problem, assignment_id: str) -> bool:
    """Stop tracking an assignment.  The last generated availability is left
    in place (it remains hand-editable on the resource); only the link to the
    template is removed."""
    state = storage.load_calendar_state(problem.id)
    before = len(state["assignments"])
    state["assignments"] = [a for a in state["assignments"]
                            if a.get("id") != assignment_id]
    if len(state["assignments"]) == before:
        return False
    storage.save_calendar_state(problem.id, state)
    return True


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #

def _check(problem_version: Optional[int], calendar_version: Optional[int],
           cur_problem_version: int, cur_calendar_version: int) -> List[str]:
    reasons: List[str] = []
    if calendar_version is None:
        reasons.append("结果早于版本追踪，无法确认是否基于当前班次")
    elif calendar_version != cur_calendar_version:
        reasons.append(f"班次日历已更新（日历 v{calendar_version} → v{cur_calendar_version}）")
    if problem_version is not None and problem_version != cur_problem_version:
        reasons.append(f"问题数据已修改（问题 v{problem_version} → v{cur_problem_version}）")
    return reasons


def staleness(problem: models.Problem) -> Dict[str, Any]:
    """Compare every derived artefact's provenance stamps against the current
    problem/calendar versions.  Old results are never deleted — they are
    surfaced with human-readable reasons so nobody executes a stale schedule.
    """
    state = storage.load_calendar_state(problem.id)
    cur_cv = int(state.get("version", 0))
    cur_pv = problem.version

    def entry(art_id: str, created_at: str, extra: Dict[str, Any],
              pv: Optional[int], cv: Optional[int]) -> Dict[str, Any]:
        reasons = _check(pv, cv, cur_pv, cur_cv)
        return {"id": art_id, "created_at": created_at, "stale": bool(reasons),
                "reasons": reasons, **extra}

    solutions = []
    for meta in storage.list_solutions(problem.id):
        sol = storage.load_solution(problem.id, meta["id"])
        if sol is None:
            continue
        solutions.append(entry(sol.id, sol.created_at,
                               {"solver": sol.solver, "status": sol.status,
                                "objective_value": sol.objective_value},
                               sol.problem_version, sol.calendar_version))

    sensitivity = [
        entry(d.get("id"), d.get("created_at", ""),
              {"solver": d.get("solver"), "parameter": d.get("parameter")},
              d.get("problem_version"), d.get("calendar_version"))
        for d in storage.list_sensitivity(problem.id)
    ]
    reports = [
        entry(d.get("id"), d.get("created_at", ""),
              {"title": d.get("title")},
              d.get("problem_version"), d.get("calendar_version"))
        for d in storage.list_reports(problem.id)
    ]

    return {
        "problem_id": problem.id,
        "problem_version": cur_pv,
        "calendar_version": cur_cv,
        "solutions": solutions,
        "sensitivity": sensitivity,
        "reports": reports,
        "n_stale": (sum(1 for s in solutions if s["stale"])
                    + sum(1 for s in sensitivity if s["stale"])
                    + sum(1 for s in reports if s["stale"])),
    }
