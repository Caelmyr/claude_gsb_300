"""
Calendar / shift template engine.

The solver world is a single line of integer slots ``[0, horizon)``; the
planner's world is a repeating calendar of 白班 / 中班 / 夜班, weekends and
节假日.  This module is the bridge between the two:

* a :class:`~backend.models.ShiftTemplate` is a repeatable *daily* window
  (in slot offsets, so a 夜班 can spill past midnight);
* a :class:`~backend.models.CalendarTemplate` assigns day plans to weekdays
  and overrides specific holiday dates;
* a :class:`~backend.models.CalendarBinding` applies a calendar to one
  resource over an optional date range;
* :func:`expand_resource` / :func:`refresh_all` turn those definitions into
  the flat ``availability`` interval list every solver already understands.

The refresh routine never destroys the link to the template it generated
from.  Each generated resource carries an ``availability_meta`` block with a
``signature``; the :mod:`backend.freshness` layer compares the signature the
solutions were born with against the current one to flag stale results.

Pure datetimes / integers only -- no I/O, so it is trivially unit-testable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from . import models


# --------------------------------------------------------------------------- #
# Date / slot mapping
# --------------------------------------------------------------------------- #

def anchor_date(problem: models.Problem) -> dt.date:
    return dt.date.fromisoformat(problem.start_date)


def slot_date(problem: models.Problem, slot: int) -> dt.date:
    """Calendar date on which an integer ``slot`` falls (slot 0 = start_date)."""
    spd = problem.slots_per_day
    day_index = slot // spd
    return anchor_date(problem) + dt.timedelta(days=day_index)


def day_slot_range(problem: models.Problem, day_index: int) -> Tuple[int, int]:
    """Half-open global slot range ``[lo, hi)`` covered by day ``day_index``
    (day 0 is the ``start_date``), clipped to the planning horizon."""
    spd = problem.slots_per_day
    lo = max(0, day_index * spd)
    hi = min(problem.horizon, (day_index + 1) * spd)
    return lo, hi


def n_days(problem: models.Problem) -> int:
    """Number of whole/partial calendar days the horizon spans."""
    if problem.horizon <= 0:
        return 0
    return (problem.horizon - 1) // problem.slots_per_day + 1


# --------------------------------------------------------------------------- #
# Interval helpers
# --------------------------------------------------------------------------- #

def normalize_intervals(intervals: Optional[List[List[int]]]) -> List[List[int]]:
    """Sort, drop empties and merge adjacent/overlapping half-open intervals."""
    if not intervals:
        return []
    ivs = sorted((int(a), int(b)) for a, b in intervals if b > a)
    merged: List[List[int]] = []
    for a, b in ivs:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def segments_intersect(ivs: List[List[int]], start: int, end: int) -> bool:
    for a, b in ivs:
        if start < b and a < end:
            return True
    return False


# --------------------------------------------------------------------------- #
# Day-plan resolution
# --------------------------------------------------------------------------- #

def _resolve_plan(plan: Any) -> Optional[List[str]]:
    """Normalise a day-plan value into a shift-id list.

    Returns [] for a closed day and None for the "fall back to weekday"
    sentinel (only meaningful for holidays)."""
    if isinstance(plan, str):
        if plan == models.DAY_PLAN_CLOSED:
            return []
        if plan == models.DAY_PLAN_WEEKDAY:
            return None
        return []
    return [str(x) for x in (plan or [])]


def plan_for_date(calendar: models.CalendarTemplate,
                  date: dt.date,
                  overrides: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return the shift ids worked on ``date`` ([] == closed all day).

    Resolution order: binding-level holiday override -> template holiday ->
    template weekday row (closed if the row is missing)."""
    iso = date.isoformat()
    weekday = date.weekday()

    plan: Any = None
    if overrides and iso in overrides:
        plan = overrides[iso]
        resolved = _resolve_plan(plan)
        if resolved is not None:
            return resolved
        # "weekday" sentinel in the override -> fall through to template

    if iso in calendar.holidays:
        resolved = _resolve_plan(calendar.holidays[iso])
        if resolved is not None:
            return resolved

    resolved = _resolve_plan(calendar.weekday_plan(weekday))
    return resolved or []


# --------------------------------------------------------------------------- #
# Expansion
# --------------------------------------------------------------------------- #

def _segments_for_day_raw(problem: models.Problem,
                          shifts: Dict[str, models.ShiftTemplate],
                          shift_ids: List[str],
                          day_index: int) -> List[Tuple[int, int]]:
    """Raw (unclipped) global-slot segments of a day plan."""
    spd = problem.slots_per_day
    base = day_index * spd
    out: List[Tuple[int, int]] = []
    for sid in shift_ids:
        shift = shifts.get(sid)
        if shift is None:
            continue
        for a, b in shift.segments:
            out.append((base + int(a), base + int(b)))
    return out


def select_binding(bindings: List[models.CalendarBinding],
                   iso_date: str) -> Optional[models.CalendarBinding]:
    """Highest-priority binding covering ``iso_date`` (ties: earliest range)."""
    candidates = [b for b in bindings if b.covers(iso_date)]
    if not candidates:
        return None
    return sorted(candidates,
                  key=lambda b: (-b.priority, b.start_date or "", b.id))[0]


def expand_resource(problem: models.Problem,
                    resource_id: str,
                    bindings: Optional[List[models.CalendarBinding]] = None,
                    ) -> List[List[int]]:
    """Expand a resource's bindings into merged availability intervals over
    the whole horizon.  Days not covered by any binding are non-working.

    A night shift crossing midnight (segment end > ``slots_per_day``) spills
    into the following day; that spill is generated while processing the
    following day (from the previous day's plan), so it appears exactly once
    even when two adjacent days run the same shift."""
    calendars = problem.calendar_map()
    shifts = problem.shift_map()
    if bindings is None:
        bindings = problem.bindings_for(resource_id)

    def plan(day_index: int) -> List[str]:
        date = anchor + dt.timedelta(days=day_index)
        binding = select_binding(bindings, date.isoformat())
        if binding is None:
            return []
        calendar_obj = calendars.get(binding.calendar_id)
        if calendar_obj is None:
            return []
        return plan_for_date(calendar_obj, date, binding.holiday_overrides)

    anchor = anchor_date(problem)
    intervals: List[Tuple[int, int]] = []
    for day_index in range(n_days(problem)):
        lo, hi = day_slot_range(problem, day_index)
        if hi <= lo:
            continue
        segments = _segments_for_day_raw(problem, shifts, plan(day_index), day_index)
        if day_index > 0:
            segments += _segments_for_day_raw(problem, shifts,
                                              plan(day_index - 1), day_index - 1)
        for a, b in segments:
            ca, cb = max(a, lo), min(b, hi)
            if cb > ca:
                intervals.append((ca, cb))
    return normalize_intervals([list(iv) for iv in intervals])


def binding_signature(problem: models.Problem,
                      bindings: List[models.CalendarBinding]) -> str:
    """Stable hash of everything that influences a resource's generated
    availability: its bindings plus the shifts/calendars they reference and
    the date/horizon anchor.  Any template edit changes the signature, which
    is how stale results are detected."""
    calendars = problem.calendar_map()
    shifts = problem.shift_map()
    referenced_cals = sorted({b.calendar_id for b in bindings})
    referenced_shifts = set()
    for cid in referenced_cals:
        cal = calendars.get(cid)
        if cal is None:
            continue
        for plan in list(cal.weekday_shifts.values()) + list(cal.holidays.values()):
            referenced_shifts.update(_resolve_plan(plan) or [])
    for b in bindings:
        for plan in (b.holiday_overrides.values()
                     if isinstance(b.holiday_overrides, dict) else []):
            referenced_shifts.update(_resolve_plan(plan) or [])

    payload = {
        "start_date": problem.start_date,
        "slots_per_day": problem.slots_per_day,
        "horizon": problem.horizon,
        "bindings": [b.to_dict() for b in sorted(bindings, key=lambda b: b.id)],
        "calendars": [calendars[c].to_dict() for c in referenced_cals],
        "shifts": [shifts[s].to_dict() for s in sorted(referenced_shifts)
                   if s in shifts],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _meta(problem: models.Problem,
          resource_id: str,
          bindings: List[models.CalendarBinding],
          intervals: List[List[int]]) -> Dict[str, Any]:
    return {
        "source": "calendar",
        "binding_ids": [b.id for b in bindings],
        "calendar_ids": sorted({b.calendar_id for b in bindings}),
        "signature": binding_signature(problem, bindings),
        "n_intervals": len(intervals),
        "generated_at": models.now_iso(),
    }


def preview_refresh(problem: models.Problem,
                    resource_ids: Optional[List[str]] = None
                    ) -> List[Dict[str, Any]]:
    """Compute what :func:`refresh_all` *would* write, without mutating.

    One row per resource that currently has a binding: new intervals, the
    signature it would carry and whether applying them would change anything
    (including a human's manual edits)."""
    targets = resource_ids or [r.id for r in problem.resources]
    rows: List[Dict[str, Any]] = []
    for rid in targets:
        bindings = problem.bindings_for(rid)
        if not bindings:
            continue
        intervals = expand_resource(problem, rid, bindings)
        res = problem.resource_map().get(rid)
        old_meta = res.availability_meta if res else {}
        rows.append({
            "resource_id": rid,
            "intervals": intervals,
            "signature": binding_signature(problem, bindings),
            "previous_signature": old_meta.get("signature"),
            "source": old_meta.get("source", "manual"),
            "changed": normalize_intervals(res.availability if res else None) != intervals
                       or old_meta.get("signature") != binding_signature(problem, bindings),
            "manual_overwrite": old_meta.get("source") == "manual" and bool(
                normalize_intervals(res.availability if res else None)),
        })
    return rows


def refresh_all(problem: models.Problem,
                resource_ids: Optional[List[str]] = None
                ) -> Dict[str, Any]:
    """Regenerate availability for every bound resource (or a subset).

    Mutates the problem in place.  Returns a summary separating resources
    whose intervals actually changed from unchanged ones, and flagging cases
    where a manually-edited list is about to be overwritten."""
    targets = set(resource_ids) if resource_ids else {r.id for r in problem.resources}
    changed: List[Dict[str, Any]] = []
    unchanged: List[Dict[str, Any]] = []
    overwritten_manual: List[str] = []

    for res in problem.resources:
        if res.id not in targets:
            continue
        bindings = problem.bindings_for(res.id)
        if not bindings:
            continue
        before = normalize_intervals(res.availability)
        was_manual = res.availability_meta.get("source") == "manual" and bool(before)
        intervals = expand_resource(problem, res.id, bindings)
        sig = binding_signature(problem, bindings)
        if before != intervals or res.availability_meta.get("signature") != sig or was_manual:
            if was_manual:
                overwritten_manual.append(res.id)
            res.availability = intervals
            res.availability_meta = _meta(problem, res.id, bindings, intervals)
            changed.append({"resource_id": res.id,
                            "n_intervals": len(intervals),
                            "had_manual_edit": was_manual})
        else:
            # keep generated_at but refresh the rest of the meta
            res.availability_meta = _meta(problem, res.id, bindings, intervals)
            unchanged.append(res.id)

    return {
        "changed": changed,
        "unchanged": unchanged,
        "overwritten_manual": overwritten_manual,
        "n_changed": len(changed),
        "n_unchanged": len(unchanged),
    }
