"""Headless CLI for the scheduling system.

Examples::

    python cli.py seed --force
    python cli.py list
    python cli.py solve demo_jobshop --solver genetic --params generations=300
    python cli.py sensitivity demo_jobshop --kind resource_capacity --resource M1
    python cli.py report demo_jobshop
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from backend import calendar, models, report, seed, sensitivity, storage
from backend import freshness
from backend.solvers import base as solver_base


def _parse_params(items: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        # coerce obvious scalars
        for cast in (int, float):
            try:
                v = cast(v)  # type: ignore[assignment]
                break
            except ValueError:
                continue
        if v == "true":
            v = True
        elif v == "false":
            v = False
        out[k] = v
    return out


def cmd_seed(args) -> None:
    created = seed.seed_all(force=args.force)
    print(f"seeded {len(created)} instance(s): {', '.join(created) or '(none new)'}")


def cmd_list(args) -> None:
    for p in storage.list_problems():
        print(f"{p['id']:16s} v{p['version']:<3d} tasks={p['n_tasks']:<3d} "
              f"res={p['n_resources']:<3d} obj={p['objective']:20s} {p['name']}")


def cmd_show(args) -> None:
    p = storage.load_problem(args.id, args.version)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    print(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))


def cmd_solve(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    params = dict(solver_base.default_params(args.solver))
    params.update(_parse_params(args.params))
    solver = solver_base.get_solver(args.solver)
    sol = solver.solve(p, params)
    storage.save_solution(args.id, sol)
    print(f"solution {sol.id}: solver={sol.solver} status={sol.status} "
          f"objective={sol.objective_value} makespan={sol.makespan} "
          f"time={sol.solve_time}s")
    if args.json:
        print(json.dumps(sol.to_dict(), ensure_ascii=False, indent=2))


def cmd_solutions(args) -> None:
    for s in storage.list_solutions(args.id):
        print(f"{s['id']}  {s['solver']:20s} {s['status']:10s} "
              f"obj={s['objective_value']} mk={s['makespan']} t={s['solve_time']}s")


def cmd_sensitivity(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    spec: Dict[str, Any] = {"kind": args.kind}
    if args.resource:
        spec["resource"] = args.resource
    if args.task:
        spec["task"] = args.task
    result = sensitivity.run_sensitivity(p, args.solver, spec, persist=True)
    print(f"sensitivity {result.id} (base obj {result.base_objective}):")
    for v in result.variations:
        print(f"  {v['label']:24s} obj={v['objective_value']} "
              f"delta={v['delta']} status={v['status']}")


def cmd_report(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    sols = [storage.load_solution(args.id, sid) for sid in args.solutions]
    sols = [s for s in sols if s is not None]
    rep = report.generate_report(p, sols)
    print(rep.content)


# --------------------------------------------------------------------------- #
# Calendar templates & freshness
# --------------------------------------------------------------------------- #

def cmd_calendar_show(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    print(f"anchor: {p.start_date}  slots/day: {p.slots_per_day}  horizon: {p.horizon}")
    print("\n[shifts]")
    for s in p.shifts:
        segs = ", ".join(f"{a}-{b}" for a, b in s.segments)
        print(f"  {s.id:10s} {s.name:10s} {segs}")
    print("\n[calendars]")
    for c in p.calendars:
        print(f"  {c.id:14s} {c.name}")
        for w in range(7):
            print(f"      {models.WEEKDAY_NAMES[w]}: {c.weekday_plan(w)}")
        for d, plan in sorted(c.holidays.items()):
            print(f"      holiday {d}: {plan}")
    print("\n[bindings]")
    for b in p.bindings:
        print(f"  {b.id:8s} {b.resource_id:6s} -> {b.calendar_id:14s} "
              f"{b.start_date or '...'}..{b.end_date or '...'} prio={b.priority}")
    print("\n[resource availability]")
    for r in p.resources:
        meta = r.availability_meta or {}
        tag = meta.get("source", "manual")
        ivs = "(always)" if r.availability is None else \
              ", ".join(f"{a}-{b}" for a, b in r.availability) or "(closed)"
        print(f"  {r.id:6s} [{tag}] {ivs}")


def cmd_calendar_preview(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    rows = calendar.preview_refresh(p, args.resources or None)
    for row in rows:
        flag = "CHANGED" if row["changed"] else "same   "
        ivs = ", ".join(f"{a}-{b}" for a, b in row["intervals"]) or "(closed)"
        print(f"  {flag} {row['resource_id']:8s} {ivs}")
        if row["manual_overwrite"]:
            print(f"          ! would overwrite a hand-edited list")


def cmd_calendar_refresh(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    if args.dry_run:
        cmd_calendar_preview(args)
        return
    result = calendar.refresh_all(p, args.resources or None)
    storage.save_problem(p)
    print(f"refreshed -> problem v{p.version}: "
          f"{result['n_changed']} changed, {result['n_unchanged']} unchanged")
    for row in result["changed"]:
        extra = " (overwrote manual edit)" if row["had_manual_edit"] else ""
        print(f"  changed {row['resource_id']}: {row['n_intervals']} interval(s){extra}")
    summary = freshness.freshness_summary(p)
    c = summary["counts"]
    print(f"stale after refresh: {c['stale_total']} "
          f"(solutions {c['solutions_stale']}/{c['solutions_total']}, "
          f"sensitivity {c['sensitivity_stale']}/{c['sensitivity_total']}, "
          f"reports {c['reports_stale']}/{c['reports_total']})")


def _print_freshness_rows(rows, label) -> None:
    for r in rows:
        if not r["stale"]:
            continue
        state = r.get("freshness", "stale")
        rid = r.get("id", "?")
        print(f"  [{state}] {label} {rid}")
        for reason in r.get("stale_reasons", [])[:4]:
            print(f"        - {reason}")


def cmd_stale(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    summary = freshness.freshness_summary(p)
    c = summary["counts"]
    print(f"instance {p.id} v{p.version}  fingerprint={summary['current_fingerprint']}")
    print(f"solutions  : {c['solutions_stale']}/{c['solutions_total']} need attention")
    print(f"sensitivity: {c['sensitivity_stale']}/{c['sensitivity_total']} need attention")
    print(f"reports    : {c['reports_stale']}/{c['reports_total']} need attention")
    if not c["stale_total"]:
        print("all results are current.")
        return
    print("\nstale / unverified artefacts:")
    _print_freshness_rows(summary["solutions"], "solution")
    _print_freshness_rows(summary["sensitivity"], "sensitivity")
    _print_freshness_rows(summary["reports"], "report")


def main() -> None:
    parser = argparse.ArgumentParser(description="OR scheduling CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_seed = sub.add_parser("seed")
    p_seed.add_argument("--force", action="store_true")
    p_seed.set_defaults(func=cmd_seed)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show")
    p_show.add_argument("id")
    p_show.add_argument("--version", type=int)
    p_show.set_defaults(func=cmd_show)

    p_solve = sub.add_parser("solve")
    p_solve.add_argument("id")
    p_solve.add_argument("--solver", default="greedy")
    p_solve.add_argument("--params", nargs="*", default=[])
    p_solve.add_argument("--json", action="store_true")
    p_solve.set_defaults(func=cmd_solve)

    p_sol = sub.add_parser("solutions")
    p_sol.add_argument("id")
    p_sol.set_defaults(func=cmd_solutions)

    p_sens = sub.add_parser("sensitivity")
    p_sens.add_argument("id")
    p_sens.add_argument("--kind", default="resource_capacity")
    p_sens.add_argument("--resource")
    p_sens.add_argument("--task")
    p_sens.add_argument("--solver", default="greedy")
    p_sens.set_defaults(func=cmd_sensitivity)

    p_rep = sub.add_parser("report")
    p_rep.add_argument("id")
    p_rep.add_argument("--solutions", nargs="*", default=[])
    p_rep.set_defaults(func=cmd_report)

    p_cal = sub.add_parser("calendar", help="show shifts, calendars, bindings")
    p_cal.add_argument("id")
    p_cal.set_defaults(func=cmd_calendar_show)

    p_prev = sub.add_parser("calendar-preview", help="dry-run template expansion")
    p_prev.add_argument("id")
    p_prev.add_argument("--resources", nargs="*", default=None)
    p_prev.set_defaults(func=cmd_calendar_preview)

    p_ref = sub.add_parser("calendar-refresh",
                           help="regenerate availability from templates and show stale results")
    p_ref.add_argument("id")
    p_ref.add_argument("--resources", nargs="*", default=None)
    p_ref.add_argument("--dry-run", action="store_true")
    p_ref.set_defaults(func=cmd_calendar_refresh)

    p_stale = sub.add_parser("stale", help="list stale/unverified solutions, analyses and reports")
    p_stale.add_argument("id")
    p_stale.set_defaults(func=cmd_stale)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
