"""Headless CLI for the scheduling system.

Examples::

    python cli.py seed --force
    python cli.py list
    python cli.py solve demo_jobshop --solver genetic --params generations=300
    python cli.py sensitivity demo_jobshop --kind resource_capacity --resource M1
    python cli.py report demo_jobshop
    python cli.py calendar-presets
    python cli.py calendar-apply demo_jobshop <template_id> M1 M2
    python cli.py calendar-refresh <template_id>
    python cli.py staleness demo_jobshop
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from backend import calendar as cal
from backend import models, report, seed, sensitivity, storage
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
    sol.problem_version = p.version
    sol.calendar_version = storage.calendar_version(args.id)
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
    result.problem_version = p.version
    result.calendar_version = storage.calendar_version(args.id)
    storage.save_sensitivity(args.id, result)
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
    rep.problem_version = p.version
    rep.calendar_version = storage.calendar_version(args.id)
    storage.save_report(args.id, rep)
    print(rep.content)


def cmd_calendar_presets(args) -> None:
    for p in cal.preset_templates():
        print(f"{p['key']:14s} {p['name']}  -- {p['description']}")


def cmd_calendar_new(args) -> None:
    """Create a template from a built-in preset key."""
    preset = next((p for p in cal.preset_templates() if p["key"] == args.preset), None)
    if preset is None:
        print(f"unknown preset: {args.preset}")
        sys.exit(1)
    data = {k: v for k, v in preset.items() if k != "key"}
    data["id"] = args.id or models.new_id("cal_t")
    template = models.CalendarTemplate.from_dict(data)
    errors = template.validate()
    if errors:
        print("invalid template:", "; ".join(errors))
        sys.exit(1)
    storage.save_calendar_template(template)
    print(f"created template {template.id} ({template.name})")


def cmd_calendar_list(args) -> None:
    for t in storage.list_calendar_templates():
        print(f"{t['id']:16s} v{t.get('version', 1):<3d} {t.get('name', '')}  "
              f"shifts={len(t.get('shifts', []))} holidays={len(t.get('holidays', []))}")


def cmd_calendar_apply(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    template = storage.load_calendar_template(args.template)
    if template is None:
        print(f"no such template: {args.template}")
        sys.exit(1)
    assignment, updated = cal.apply_template(
        p, template, args.resources,
        start_date=args.start_date, end_date=args.end_date)
    print(f"applied template {template.id} v{template.version} to "
          f"{len(updated)} resource(s): {', '.join(updated)}")
    print(f"assignment {assignment.id}; problem now v{p.version}, "
          f"calendar v{storage.calendar_version(p.id)}")


def cmd_calendar_refresh(args) -> None:
    summary = cal.refresh_template(args.template)
    if not summary:
        print("no assignments reference this template")
        return
    for s in summary:
        print(f"{s['problem_id']:16s} assignments={s['assignments_refreshed']} "
              f"resources={len(s['resources'])} calendar_v={s['calendar_version']} "
              f"stale: solutions={s['stale_solutions']} "
              f"sensitivity={s['stale_sensitivity']} reports={s['stale_reports']}")


def cmd_staleness(args) -> None:
    p = storage.load_problem(args.id)
    if p is None:
        print(f"no such problem: {args.id}")
        sys.exit(1)
    st = cal.staleness(p)
    print(f"problem v{st['problem_version']}, calendar v{st['calendar_version']}, "
          f"stale artefacts: {st['n_stale']}")
    for group in ("solutions", "sensitivity", "reports"):
        for item in st[group]:
            mark = "STALE" if item["stale"] else "ok   "
            print(f"  [{mark}] {group[:-1]} {item['id']}"
                  + (f"  -- {'; '.join(item['reasons'])}" if item["stale"] else ""))


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

    p_cp = sub.add_parser("calendar-presets")
    p_cp.set_defaults(func=cmd_calendar_presets)

    p_cn = sub.add_parser("calendar-new")
    p_cn.add_argument("preset", help="preset key (see calendar-presets)")
    p_cn.add_argument("--id")
    p_cn.set_defaults(func=cmd_calendar_new)

    p_cl = sub.add_parser("calendar-list")
    p_cl.set_defaults(func=cmd_calendar_list)

    p_ca = sub.add_parser("calendar-apply")
    p_ca.add_argument("id")
    p_ca.add_argument("template")
    p_ca.add_argument("resources", nargs="+")
    p_ca.add_argument("--start-date")
    p_ca.add_argument("--end-date")
    p_ca.set_defaults(func=cmd_calendar_apply)

    p_cr = sub.add_parser("calendar-refresh")
    p_cr.add_argument("template")
    p_cr.set_defaults(func=cmd_calendar_refresh)

    p_st = sub.add_parser("staleness")
    p_st.add_argument("id")
    p_st.set_defaults(func=cmd_staleness)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
