"""
Domain model for the OR / scheduling solver system.

The problem is a resource-constrained project scheduling problem (RCPSP)
generalised to three resource classes (personnel, equipment, time) and to
arbitrary hard/soft constraints.  Time is discretised into integer slots so
that a time-indexed LP/IP formulation and discrete meta-heuristics share one
representation.

Every entity is a plain dataclass that round-trips through JSON.  This module
deliberately has no I/O and no solver logic: it is only the shared vocabulary
used by the storage layer, the solvers and the web API.
"""

from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Enumerated vocabulary
# --------------------------------------------------------------------------- #

RESOURCE_TYPES = ("personnel", "equipment", "time")

# Monday == 0 ... Sunday == 6 (matches ``datetime.date.weekday()``).
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# Special values allowed in ``CalendarTemplate.weekday_shifts`` / ``holidays``:
#   a list of shift ids  -> those shifts are worked that day
#   "closed"             -> the resource is unavailable all day
#   "weekday"            -> (holidays only) fall back to the weekday plan
DAY_PLAN_CLOSED = "closed"
DAY_PLAN_WEEKDAY = "weekday"

OBJECTIVE_TYPES = (
    "makespan",                 # min max completion time
    "total_completion",         # min sum of completion times
    "weighted_completion",      # min sum weight_i * C_i
    "tardiness",                # min sum weight_i * max(0, C_i - due_i)
    "cost",                     # min resource usage cost
    "custom",                   # weighted combination of the primitives
)

# Hard constraints: a violation makes a schedule *infeasible*.
HARD_CONSTRAINT_TYPES = (
    "precedence",               # task A before task B
    "time_window",              # start within [release, deadline]
    "fixed_start",              # task must start at exactly t
    "resource_capacity",        # implied by resource.capacity, still expressible
    "non_overlap",              # two tasks may not overlap on a resource
    "max_concurrent",           # at most k tasks running simultaneously
    "resource_assignment",      # task requires specific resource set
)

# Soft constraints: a violation contributes a *penalty* to the objective.
SOFT_CONSTRAINT_TYPES = (
    "due_date",                 # penalise late completion (weighted tardiness)
    "preferred_window",         # penalise starts outside [a, b]
    "min_gap",                  # penalise insufficient gap between two tasks
    "resource_balance",         # penalise uneven resource load
    "setup_time",               # penalise missing setup between consecutive jobs
    "max_makespan",             # penalise exceeding a target makespan
)

SOLVER_NAMES = ("lp", "ip", "genetic", "simulated_annealing", "greedy")

SOLUTION_STATUS = ("optimal", "feasible", "infeasible", "timeout", "error")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------- #
# Resource
# --------------------------------------------------------------------------- #

@dataclass
class Resource:
    """A schedulable resource.

    ``availability`` is a list of half-open ``[start, end)`` intervals during
    which the resource is usable; ``None`` means always available.  ``skills``
    is used by the ``resource_assignment`` constraint so a task can require a
    person or machine that possesses a particular capability rather than one
    specific instance.

    ``availability_meta`` records *how* the availability list got there.  It is
    managed by the calendar engine (``backend.calendar``) and never seen by the
    solvers:

    * ``source`` is ``calendar`` when the intervals were generated from a
      calendar binding, or ``manual`` when a human edited the list by hand;
    * ``binding_id`` / ``calendar_id`` point back at the generating binding;
    * ``signature`` is a cheap hash of the binding + templates at generation
      time, so the refresh routine can spot "generated, but template changed"
      without re-expanding anything;
    * ``generated_at`` is when the intervals were last generated.
    """
    id: str
    name: str = ""
    type: str = "personnel"                 # personnel | equipment | time
    capacity: float = 1.0                   # units available simultaneously
    skills: List[str] = field(default_factory=list)
    cost_per_unit: float = 0.0              # cost per unit-time of use
    availability: Optional[List[List[int]]] = None   # [[start, end), ...]
    availability_meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in RESOURCE_TYPES:
            raise ValueError(f"unknown resource type: {self.type}")
        if self.capacity < 0:
            raise ValueError(f"resource {self.id}: negative capacity")

    @property
    def availability_source(self) -> str:
        return (self.availability_meta or {}).get("source", "manual")

    def available_at(self, t: int) -> bool:
        if self.availability is None:
            return True
        return any(a <= t < b for (a, b) in self.availability)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Resource":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**d)


# --------------------------------------------------------------------------- #
# Shift / calendar templates
# --------------------------------------------------------------------------- #

@dataclass
class ShiftTemplate:
    """A repeatable daily work window, e.g. 白班 / 中班 / 夜班.

    ``segments`` are half-open ``[start, end)`` ranges expressed in *slot
    offsets within a day* (0 .. ``slots_per_day``); a value larger than
    ``slots_per_day`` is allowed on ``start`` of a segment so that a 夜班 can
    spill past midnight (e.g. start=22, end=26 with 24 slots/day).  A day
    plan referencing several shifts simply works the union of their segments.
    """
    id: str
    name: str = ""
    color: str = ""
    segments: List[List[int]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ShiftTemplate":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**d)


@dataclass
class CalendarTemplate:
    """A weekly rhythm plus holiday overrides.

    ``weekday_shifts`` maps a weekday index (0=Mon .. 6=Sun) to either a list
    of shift ids or the sentinel ``"closed"``.  ``holidays`` maps an ISO date
    string (``YYYY-MM-DD``) to a plan: list of shift ids, ``"closed"`` or
    ``"weekday"`` (honour whatever the weekday row says).
    """
    id: str
    name: str = ""
    description: str = ""
    weekday_shifts: Dict[str, Any] = field(default_factory=dict)
    holidays: Dict[str, Any] = field(default_factory=dict)

    def weekday_plan(self, weekday: int) -> Any:
        return self.weekday_shifts.get(str(weekday),
                                      self.weekday_shifts.get(weekday, DAY_PLAN_CLOSED))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CalendarTemplate":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**d)


@dataclass
class CalendarBinding:
    """Apply a calendar template to one resource over a date range.

    Several bindings may target the same resource for different date ranges
    (e.g. a temporary night-shift stint); ``priority`` breaks overlaps
    (higher wins).  A binding with ``start_date`` / ``end_date`` == None spans
    the whole horizon.  ``holiday_overrides`` lets one binding deviate from the
    template's global holiday list without editing the shared template.
    """
    id: str
    resource_id: str
    calendar_id: str
    start_date: Optional[str] = None         # ISO date, inclusive; None = horizon start
    end_date: Optional[str] = None           # ISO date, inclusive; None = horizon end
    priority: int = 0
    holiday_overrides: Dict[str, Any] = field(default_factory=dict)

    def covers(self, iso_date: str) -> bool:
        if self.start_date and iso_date < self.start_date:
            return False
        if self.end_date and iso_date > self.end_date:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CalendarBinding":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**d)


# --------------------------------------------------------------------------- #
# Task
# --------------------------------------------------------------------------- #

@dataclass
class Task:
    """A unit of work.  ``duration`` is an integer number of time slots and
    ``resource_requirements`` maps a resource id to the number of capacity
    units the task occupies for every slot of its execution."""
    id: str
    name: str = ""
    duration: int = 1
    resource_requirements: Dict[str, float] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)   # task ids
    release_time: int = 0
    due_date: Optional[int] = None
    weight: float = 1.0
    priority: float = 1.0                     # used by greedy / tie-breaks

    def __post_init__(self) -> None:
        if self.duration <= 0:
            raise ValueError(f"task {self.id}: duration must be positive")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        return cls(**d)


# --------------------------------------------------------------------------- #
# Constraints
# --------------------------------------------------------------------------- #

@dataclass
class HardConstraint:
    """A hard constraint.  ``params`` is a type-specific payload.

    * precedence            params: {"before": task_id, "after": task_id}
    * time_window           params: {"task": task_id, "release": t, "deadline": t}
    * fixed_start           params: {"task": task_id, "start": t}
    * non_overlap           params: {"tasks": [task_id, ...]}
    * max_concurrent        params: {"limit": int}
    * resource_assignment   params: {"task": task_id, "resources": [res_id, ...]}
    * resource_capacity     params: {"resource": res_id, "capacity": float}
    """
    id: str
    type: str
    params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in HARD_CONSTRAINT_TYPES:
            raise ValueError(f"unknown hard constraint type: {self.type}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HardConstraint":
        return cls(**d)


@dataclass
class SoftConstraint:
    """A soft constraint with a linear penalty ``penalty`` and optional weight
    ``factor`` used to scale the contribution inside the objective."""
    id: str
    type: str
    params: Dict[str, Any] = field(default_factory=dict)
    penalty: float = 1.0
    factor: float = 1.0

    def __post_init__(self) -> None:
        if self.type not in SOFT_CONSTRAINT_TYPES:
            raise ValueError(f"unknown soft constraint type: {self.type}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SoftConstraint":
        return cls(**d)


# --------------------------------------------------------------------------- #
# Objective
# --------------------------------------------------------------------------- #

@dataclass
class Objective:
    """The optimisation goal.  ``type`` selects a primitive; ``custom`` can
    combine primitives through ``weights`` (a map of primitive name -> weight).
    ``minimize`` is True for a minimisation problem."""
    type: str = "makespan"
    weights: Dict[str, float] = field(default_factory=dict)
    minimize: bool = True
    params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in OBJECTIVE_TYPES:
            raise ValueError(f"unknown objective type: {self.type}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Objective":
        return cls(**d)


# --------------------------------------------------------------------------- #
# Problem
# --------------------------------------------------------------------------- #

@dataclass
class Problem:
    id: str
    name: str = ""
    description: str = ""
    horizon: int = 100
    time_unit: str = "hour"
    start_date: str = "2024-01-01"          # ISO date that slot 0 maps to
    slots_per_day: int = 24                 # integer slots in one calendar day
    resources: List[Resource] = field(default_factory=list)
    shifts: List[ShiftTemplate] = field(default_factory=list)
    calendars: List[CalendarTemplate] = field(default_factory=list)
    bindings: List[CalendarBinding] = field(default_factory=list)
    tasks: List[Task] = field(default_factory=list)
    hard_constraints: List[HardConstraint] = field(default_factory=list)
    soft_constraints: List[SoftConstraint] = field(default_factory=list)
    objective: Objective = field(default_factory=Objective)
    version: int = 1
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    # -- convenience index helpers ---------------------------------------- #
    def resource_map(self) -> Dict[str, Resource]:
        return {r.id: r for r in self.resources}

    def shift_map(self) -> Dict[str, ShiftTemplate]:
        return {s.id: s for s in self.shifts}

    def calendar_map(self) -> Dict[str, CalendarTemplate]:
        return {c.id: c for c in self.calendars}

    def bindings_for(self, resource_id: str) -> List[CalendarBinding]:
        return sorted((b for b in self.bindings if b.resource_id == resource_id),
                      key=lambda b: (-b.priority, b.start_date or "", b.id))

    def task_map(self) -> Dict[str, Task]:
        return {t.id: t for t in self.tasks}

    def precedence_edges(self) -> List[Tuple[str, str]]:
        """Return explicit (before, after) edges from task.dependencies plus
        explicit ``precedence`` hard constraints, de-duplicated."""
        edges: List[Tuple[str, str]] = []
        seen = set()
        for t in self.tasks:
            for dep in t.dependencies:
                if (dep, t.id) not in seen:
                    seen.add((dep, t.id))
                    edges.append((dep, t.id))
        for c in self.hard_constraints:
            if c.type == "precedence":
                b, a = c.params.get("before"), c.params.get("after")
                if b and a and (b, a) not in seen:
                    seen.add((b, a))
                    edges.append((b, a))
        return edges

    def successors(self) -> Dict[str, List[str]]:
        succ: Dict[str, List[str]] = {t.id: [] for t in self.tasks}
        for b, a in self.precedence_edges():
            succ.setdefault(b, []).append(a)
        return succ

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Problem":
        d = dict(d)
        d["resources"] = [Resource.from_dict(r) for r in d.get("resources", [])]
        d["shifts"] = [ShiftTemplate.from_dict(s) for s in d.get("shifts", [])]
        d["calendars"] = [CalendarTemplate.from_dict(c)
                          for c in d.get("calendars", [])]
        d["bindings"] = [CalendarBinding.from_dict(b)
                         for b in d.get("bindings", [])]
        d["tasks"] = [Task.from_dict(t) for t in d.get("tasks", [])]
        d["hard_constraints"] = [
            HardConstraint.from_dict(c) for c in d.get("hard_constraints", [])
        ]
        d["soft_constraints"] = [
            SoftConstraint.from_dict(c) for c in d.get("soft_constraints", [])
        ]
        d["objective"] = Objective.from_dict(d.get("objective", {}))
        allowed = set(cls.__dataclass_fields__)
        d = {k: v for k, v in d.items() if k in allowed}
        return cls(**d)


# --------------------------------------------------------------------------- #
# Solution
# --------------------------------------------------------------------------- #

@dataclass
class Assignment:
    task: str
    start: int
    end: int
    resources: List[str] = field(default_factory=list)

    @property
    def duration(self) -> int:
        return self.end - self.start

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Assignment":
        return cls(**d)


@dataclass
class Solution:
    id: str
    problem_id: str
    solver: str
    status: str = "feasible"            # optimal|feasible|infeasible|timeout|error
    objective_value: Optional[float] = None
    makespan: Optional[int] = None
    assignments: List[Assignment] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    solve_time: float = 0.0
    message: str = ""
    lower_bound: Optional[float] = None
    created_at: str = field(default_factory=now_iso)
    version: int = 1
    # Provenance used by the freshness layer: problem version this artefact was
    # computed against, a fingerprint of the solver inputs, and the (possibly
    # empty) list of reasons it is stale relative to the current problem.
    # ``None`` means "produced before provenance existed" -> unverified.
    problem_version: Optional[int] = None
    input_fingerprint: Optional[str] = None
    stale_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Solution":
        d = dict(d)
        d["assignments"] = [Assignment.from_dict(a) for a in d.get("assignments", [])]
        return cls(**d)


# --------------------------------------------------------------------------- #
# Solver configuration
# --------------------------------------------------------------------------- #

@dataclass
class SolverConfig:
    id: str
    problem_id: str
    solver: str
    params: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SolverConfig":
        return cls(**d)


# --------------------------------------------------------------------------- #
# Sensitivity & report wrappers
# --------------------------------------------------------------------------- #

@dataclass
class SensitivityResult:
    id: str
    problem_id: str
    solver: str
    base_objective: float
    parameter: str
    variations: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    problem_version: Optional[int] = None
    input_fingerprint: Optional[str] = None
    spec: Dict[str, Any] = field(default_factory=dict)
    stale_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SensitivityResult":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**d)


@dataclass
class Report:
    id: str
    problem_id: str
    title: str = ""
    format: str = "markdown"
    content: str = ""
    created_at: str = field(default_factory=now_iso)
    problem_version: Optional[int] = None
    input_fingerprint: Optional[str] = None
    solution_ids: List[str] = field(default_factory=list)
    sensitivity_id: Optional[str] = None
    stale_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Report":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**d)


# --------------------------------------------------------------------------- #
# Cross-cutting validation used by storage and API
# --------------------------------------------------------------------------- #

def _plan_shift_ids(plan: Any) -> List[str]:
    """Extract referenced shift ids from a day-plan value (list / sentinel)."""
    if isinstance(plan, (list, tuple)):
        return [str(x) for x in plan]
    return []


def validate_problem(problem: Problem) -> List[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errors: List[str] = []

    ids = [t.id for t in problem.tasks]
    if len(ids) != len(set(ids)):
        errors.append("task ids must be unique")

    rids = [r.id for r in problem.resources]
    if len(rids) != len(set(rids)):
        errors.append("resource ids must be unique")

    sids = [s.id for s in problem.shifts]
    if len(sids) != len(set(sids)):
        errors.append("shift ids must be unique")
    if problem.slots_per_day <= 0:
        errors.append("slots_per_day must be positive")
    import datetime as _dt
    try:
        _dt.date.fromisoformat(problem.start_date)
    except (ValueError, TypeError):
        errors.append(f"start_date must be ISO YYYY-MM-DD: {problem.start_date!r}")
    for s in problem.shifts:
        if not s.segments:
            errors.append(f"shift {s.id}: at least one segment is required")
        for a, b in s.segments:
            if a < 0 or b <= a:
                errors.append(f"shift {s.id}: segment [{a}, {b}) is invalid")

    cids = [c.id for c in problem.calendars]
    if len(cids) != len(set(cids)):
        errors.append("calendar ids must be unique")
    shift_set = set(sids)
    for c in problem.calendars:
        for key, plan in list(c.weekday_shifts.items()) + list(c.holidays.items()):
            for ref in _plan_shift_ids(plan):
                if ref not in shift_set:
                    errors.append(f"calendar {c.id}: unknown shift '{ref}'")

    tasks = problem.task_map()
    res = problem.resource_map()
    cal_set = set(cids)

    bids = [b.id for b in problem.bindings]
    if len(bids) != len(set(bids)):
        errors.append("binding ids must be unique")
    for b in problem.bindings:
        if b.resource_id not in res:
            errors.append(f"binding {b.id}: unknown resource '{b.resource_id}'")
        if b.calendar_id not in cal_set:
            errors.append(f"binding {b.id}: unknown calendar '{b.calendar_id}'")
        if b.start_date and b.end_date and b.start_date > b.end_date:
            errors.append(f"binding {b.id}: start_date after end_date")
        for ref in _plan_shift_ids(b.holiday_overrides.values()
                                   if isinstance(b.holiday_overrides, dict)
                                   else []):
            if ref not in shift_set:
                errors.append(f"binding {b.id}: unknown shift '{ref}'")

    for t in problem.tasks:
        if t.duration <= 0:
            errors.append(f"task {t.id}: duration must be > 0")
        if t.release_time < 0:
            errors.append(f"task {t.id}: release_time must be >= 0")
        if t.release_time + t.duration > problem.horizon:
            errors.append(f"task {t.id}: release + duration exceeds horizon")
        for dep in t.dependencies:
            if dep not in tasks:
                errors.append(f"task {t.id}: unknown dependency '{dep}'")
        for r, amount in t.resource_requirements.items():
            if r not in res:
                errors.append(f"task {t.id}: unknown resource '{r}'")
            if amount < 0:
                errors.append(f"task {t.id}: negative requirement for '{r}'")

    # dependency cycle detection (Kahn's algorithm)
    succ = problem.successors()
    indeg = {i: 0 for i in ids}
    for b, a in problem.precedence_edges():
        if b not in indeg or a not in indeg:
            continue
        indeg[a] += 1
    queue = [i for i in ids if indeg.get(i, 0) == 0]
    topo_count = 0
    while queue:
        n = queue.pop()
        topo_count += 1
        for s in succ.get(n, []):
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    if topo_count != len(ids):
        errors.append("dependency graph contains a cycle")

    for c in problem.hard_constraints:
        p = c.params
        if c.type == "precedence":
            if p.get("before") not in tasks or p.get("after") not in tasks:
                errors.append(f"precedence '{c.id}': unknown task reference")
        elif c.type in ("time_window", "fixed_start", "resource_assignment"):
            if p.get("task") not in tasks:
                errors.append(f"{c.type} '{c.id}': unknown task '{p.get('task')}'")
        elif c.type == "resource_capacity":
            if p.get("resource") not in res:
                errors.append(f"resource_capacity '{c.id}': unknown resource")

    if problem.horizon <= 0:
        errors.append("horizon must be positive")

    return errors


def objective_summary(problem: Problem) -> str:
    """Short human-readable objective label for UI titles."""
    o = problem.objective
    if o.type == "custom":
        parts = ", ".join(f"{k}={v}" for k, v in sorted(o.weights.items()))
        return f"custom({parts})"
    return o.type
