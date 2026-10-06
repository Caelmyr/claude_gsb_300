"""
Result freshness ("这条结果还成立吗？").

Every solver artefact (solution, sensitivity run, report) is born with two
pieces of provenance:

* ``problem_version`` -- which ``problem.json`` version was current;
* ``input_fingerprint`` -- a hash of the *solver-relevant* inputs of that
  version (availability calendars, capacities, tasks, constraints, ...).

After a calendar refresh or any other edit, :func:`evaluate_*` recomputes the
fingerprint of the *current* problem and, when they differ, diffs the archived
version against the current one to explain *why*.  The UI never has to guess:
every list of results carries an explicit state:

* ``current``    -- inputs unchanged, the result still stands;
* ``stale``      -- inputs changed (with human-readable ``stale_reasons``);
* ``unverified`` -- the artefact predates provenance tracking.

Reports additionally inherit staleness from the solutions / sensitivity run
they were assembled from, so a report built on a stale plan is marked stale
even when its own snapshot comparison is inconclusive.

The artefact files themselves are never rewritten: historical results stay
immutable, freshness is computed on read.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from . import calendar, models, storage


# --------------------------------------------------------------------------- #
# Fingerprinting
# --------------------------------------------------------------------------- #

def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def input_fingerprint(problem: models.Problem) -> str:
    """Hash of everything that can change an optimal schedule.

    Shift/calendar *definitions* only enter through the availability intervals
    they produced (plus their generation signatures), so the digest is computed
    against exactly the data the solvers see.  It therefore also detects plain
    hand edits to an availability list."""
    payload = {
        "horizon": problem.horizon,
        "slots_per_day": problem.slots_per_day,
        "resources": [
            {
                "id": r.id,
                "type": r.type,
                "capacity": r.capacity,
                "cost_per_unit": r.cost_per_unit,
                "skills": sorted(r.skills),
                "availability": calendar.normalize_intervals(r.availability),
                "availability_signature": (r.availability_meta or {}).get("signature"),
            }
            for r in problem.resources
        ],
        "tasks": [
            {
                "id": t.id,
                "duration": t.duration,
                "requirements": t.resource_requirements,
                "dependencies": t.dependencies,
                "release_time": t.release_time,
                "due_date": t.due_date,
                "weight": t.weight,
                "priority": t.priority,
            }
            for t in problem.tasks
        ],
        "hard_constraints": [c.to_dict() for c in problem.hard_constraints],
        "soft_constraints": [c.to_dict() for c in problem.soft_constraints],
        "objective": problem.objective.to_dict(),
    }
    blob = _canonical(payload).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def stamp_solution(problem: models.Problem,
                   solution: models.Solution) -> models.Solution:
    solution.problem_version = problem.version
    solution.input_fingerprint = input_fingerprint(problem)
    solution.stale_reasons = []
    return solution


def stamp_sensitivity(problem: models.Problem,
                      result: models.SensitivityResult) -> models.SensitivityResult:
    result.problem_version = problem.version
    result.input_fingerprint = input_fingerprint(problem)
    result.stale_reasons = []
    return result


def stamp_report(problem: models.Problem,
                 report: models.Report,
                 solution_ids: Optional[List[str]] = None,
                 sensitivity_id: Optional[str] = None) -> models.Report:
    report.problem_version = problem.version
    report.input_fingerprint = input_fingerprint(problem)
    report.solution_ids = list(solution_ids or report.solution_ids)
    report.sensitivity_id = sensitivity_id or report.sensitivity_id
    report.stale_reasons = []
    return report


# --------------------------------------------------------------------------- #
# Structural diff: explain *what* changed between two problem versions
# --------------------------------------------------------------------------- #

def _index(items: List[Any], key: str = "id") -> Dict[str, Any]:
    return {getattr(x, key): x for x in items}


def diff_problems(old: models.Problem, new: models.Problem) -> List[str]:
    """Human-readable list of solver-relevant differences (empty == same)."""
    reasons: List[str] = []

    old_res, new_res = _index(old.resources), _index(new.resources)
    for rid in sorted(set(old_res) | set(new_res)):
        o, n = old_res.get(rid), new_res.get(rid)
        if o is None:
            reasons.append(f"新增资源 {rid}")
            continue
        if n is None:
            reasons.append(f"删除资源 {rid}")
            continue
        if calendar.normalize_intervals(o.availability) != \
                calendar.normalize_intervals(n.availability):
            reasons.append(f"资源 {rid} 的可用时段（班次）已变化")
        if o.capacity != n.capacity:
            reasons.append(f"资源 {rid} 容量 {o.capacity} → {n.capacity}")
        if o.type != n.type:
            reasons.append(f"资源 {rid} 类型变化")
        if o.cost_per_unit != n.cost_per_unit:
            reasons.append(f"资源 {rid} 单位成本变化")

    old_tasks, new_tasks = _index(old.tasks), _index(new.tasks)
    for tid in sorted(set(old_tasks) | set(new_tasks)):
        o, n = old_tasks.get(tid), new_tasks.get(tid)
        if o is None:
            reasons.append(f"新增任务 {tid}")
            continue
        if n is None:
            reasons.append(f"删除任务 {tid}")
            continue
        if o.duration != n.duration:
            reasons.append(f"任务 {tid} 工期 {o.duration} → {n.duration}")
        if o.resource_requirements != n.resource_requirements:
            reasons.append(f"任务 {tid} 资源需求变化")
        if o.dependencies != n.dependencies:
            reasons.append(f"任务 {tid} 依赖关系变化")
        if o.release_time != n.release_time:
            reasons.append(f"任务 {tid} 最早开始时间变化")
        if o.due_date != n.due_date:
            reasons.append(f"任务 {tid} 截止日期变化")

    if old.horizon != new.horizon:
        reasons.append(f"计划周期 {old.horizon} → {new.horizon}")
    if old.objective.to_dict() != new.objective.to_dict():
        reasons.append("目标函数变化")
    if [c.to_dict() for c in old.hard_constraints] != \
            [c.to_dict() for c in new.hard_constraints]:
        reasons.append("硬约束变化")
    if [c.to_dict() for c in old.soft_constraints] != \
            [c.to_dict() for c in new.soft_constraints]:
        reasons.append("软约束变化")
    return reasons


def _archived(problem_id: str, version: Optional[int]) -> Optional[models.Problem]:
    if version is None:
        return None
    return storage.load_problem(problem_id, version)


# --------------------------------------------------------------------------- #
# State evaluation
# --------------------------------------------------------------------------- #

def _state(fingerprint: Optional[str], version: Optional[int],
           current: models.Problem,
           old: Optional[models.Problem]) -> Dict[str, Any]:
    if fingerprint is None or version is None:
        return {"state": "unverified", "stale": True, "reasons": ["结果生成于版本追溯功能上线前，无法自动核对"]}
    if fingerprint == input_fingerprint(current):
        return {"state": "current", "stale": False, "reasons": []}
    reasons: List[str] = []
    if old is None:
        reasons.append(f"生成时所用实例版本 v{version} 已不可考，输入指纹不一致")
    else:
        reasons = diff_problems(old, current)
        if not reasons:
            reasons.append("输入指纹不一致")
    return {"state": "stale", "stale": True, "reasons": reasons}


def evaluate_solution(current: models.Problem,
                      sol: models.Solution) -> Dict[str, Any]:
    old = _archived(current.id, sol.problem_version)
    info = _state(sol.input_fingerprint, sol.problem_version, current, old)
    return {
        "id": sol.id,
        "solver": sol.solver,
        "status": sol.status,
        "objective_value": sol.objective_value,
        "makespan": sol.makespan,
        "solve_time": sol.solve_time,
        "created_at": sol.created_at,
        "n_assignments": len(sol.assignments),
        "problem_version": sol.problem_version,
        "freshness": info["state"],
        "stale": info["stale"],
        "stale_reasons": info["reasons"],
    }


def evaluate_sensitivity(current: models.Problem,
                         result: models.SensitivityResult) -> Dict[str, Any]:
    old = _archived(current.id, result.problem_version)
    info = _state(result.input_fingerprint, result.problem_version, current, old)
    return {
        **result.to_dict(),
        "freshness": info["state"],
        "stale": info["stale"],
        "stale_reasons": info["reasons"],
    }


def evaluate_report(current: models.Problem,
                    rep: models.Report,
                    solution_states: Optional[Dict[str, Dict[str, Any]]] = None,
                    sensitivity_states: Optional[Dict[str, Dict[str, Any]]] = None
                    ) -> Dict[str, Any]:
    old = _archived(current.id, rep.problem_version)
    info = _state(rep.input_fingerprint, rep.problem_version, current, old)
    reasons = list(info["reasons"])
    state = info["state"]

    # Inherited staleness: a report is only as fresh as its ingredients.
    for sid in rep.solution_ids:
        st = (solution_states or {}).get(sid)
        if st and st.get("stale"):
            reasons.append(f"包含的方案 {sid} 已失效（{'; '.join(st.get('stale_reasons', [])[:2])}）")
            if state == "current":
                state = "stale"
    if rep.sensitivity_id:
        st = (sensitivity_states or {}).get(rep.sensitivity_id)
        if st and st.get("stale"):
            reasons.append("包含的敏感性分析已失效")
            if state == "current":
                state = "stale"

    # De-duplicate while preserving order.
    seen = set()
    reasons = [r for r in reasons if not (r in seen or seen.add(r))]
    return {
        **rep.to_dict(),
        "freshness": state,
        "stale": state != "current",
        "stale_reasons": reasons,
    }


# --------------------------------------------------------------------------- #
# Bulk summary used by the dashboard banner / list endpoints
# --------------------------------------------------------------------------- #

def freshness_summary(current: models.Problem) -> Dict[str, Any]:
    """One call that evaluates every persisted artefact of an instance."""
    solutions_raw = [storage.load_solution(current.id, row["id"])
                     for row in storage.list_solutions(current.id)]
    solutions_raw = [s for s in solutions_raw if s is not None]
    sol_states = {s.id: evaluate_solution(current, s) for s in solutions_raw}

    sens_states = {}
    for item in storage.list_sensitivity(current.id):
        result = models.SensitivityResult.from_dict(item)
        sens_states[result.id] = evaluate_sensitivity(current, result)

    rep_states = {}
    for item in storage.list_reports(current.id):
        rep = models.Report.from_dict(item)
        rep_states[rep.id] = evaluate_report(
            current, rep, sol_states, sens_states)

    stale_solutions = [v for v in sol_states.values() if v["stale"]]
    stale_sens = [v for v in sens_states.values() if v["stale"]]
    stale_reps = [v for v in rep_states.values() if v["stale"]]
    return {
        "problem_id": current.id,
        "problem_version": current.version,
        "current_fingerprint": input_fingerprint(current),
        "solutions": list(sol_states.values()),
        "sensitivity": list(sens_states.values()),
        "reports": list(rep_states.values()),
        "counts": {
            "solutions_total": len(sol_states),
            "solutions_stale": len(stale_solutions),
            "sensitivity_total": len(sens_states),
            "sensitivity_stale": len(stale_sens),
            "reports_total": len(rep_states),
            "reports_stale": len(stale_reps),
            "stale_total": len(stale_solutions) + len(stale_sens) + len(stale_reps),
        },
    }
