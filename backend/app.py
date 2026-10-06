"""Flask application: static frontend + JSON REST API.

The API is a thin, stateless layer over :mod:`storage`, :mod:`models`, the
solvers and the analysis helpers.  All mutation goes through the storage layer
so the file-locking / atomic-write / versioning guarantees hold regardless of
whether a request arrives from the UI or the CLI.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory

from . import calendar as cal
from . import models, report, sensitivity, storage
from .solvers import base as solver_base

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "frontend")


def create_app() -> Flask:
    app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="/static")
    storage.ensure_dirs()

    # ------------------------------------------------------------------ #
    # Static pages
    # ------------------------------------------------------------------ #
    @app.route("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.route("/<path:name>")
    def pages(name: str):
        # Serve any .html page or asset; fall back to index for SPA-like nav.
        path = os.path.join(FRONTEND_DIR, name)
        if os.path.isfile(path):
            return send_from_directory(FRONTEND_DIR, name)
        if name.endswith(".html"):
            return send_from_directory(FRONTEND_DIR, name.split("/")[-1])
        return jsonify({"error": "not found"}), 404

    # ------------------------------------------------------------------ #
    # Meta
    # ------------------------------------------------------------------ #
    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/api/solvers")
    def solvers():
        return jsonify({
            "solvers": solver_base.available_solvers(),
            "defaults": {n: solver_base.default_params(n)
                         for n in solver_base.available_solvers()},
        })

    # ------------------------------------------------------------------ #
    # Problems
    # ------------------------------------------------------------------ #
    @app.route("/api/problems", methods=["GET"])
    def list_problems():
        return jsonify({"problems": storage.list_problems()})

    @app.route("/api/problems", methods=["POST"])
    def create_problem():
        data = request.get_json(force=True)
        try:
            problem = models.Problem.from_dict(data)
            if not problem.id:
                problem.id = models.new_id("prob")
            errors = models.validate_problem(problem)
            if errors:
                return jsonify({"error": "validation failed", "details": errors}), 400
            storage.save_problem(problem)
            return jsonify(problem.to_dict()), 201
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/problems/<problem_id>", methods=["GET"])
    def get_problem(problem_id: str):
        version = request.args.get("version", type=int)
        problem = storage.load_problem(problem_id, version)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(problem.to_dict())

    @app.route("/api/problems/<problem_id>", methods=["PUT"])
    def update_problem(problem_id: str):
        data = request.get_json(force=True)
        try:
            problem = models.Problem.from_dict(data)
            problem.id = problem_id
            errors = models.validate_problem(problem)
            if errors:
                return jsonify({"error": "validation failed", "details": errors}), 400
            storage.save_problem(problem)
            return jsonify(problem.to_dict())
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/problems/<problem_id>", methods=["DELETE"])
    def delete_problem(problem_id: str):
        if storage.delete_problem(problem_id):
            return jsonify({"ok": True})
        return jsonify({"error": "not found"}), 404

    @app.route("/api/problems/<problem_id>/versions", methods=["GET"])
    def versions(problem_id: str):
        return jsonify({"versions": storage.list_versions(problem_id)})

    @app.route("/api/problems/<problem_id>/versions/<int:version>", methods=["GET"])
    def version(problem_id: str, version: int):
        problem = storage.load_problem(problem_id, version)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(problem.to_dict())

    # ------------------------------------------------------------------ #
    # Solving
    # ------------------------------------------------------------------ #
    @app.route("/api/problems/<problem_id>/solve", methods=["POST"])
    def solve(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True) or {}
        solver_name = data.get("solver", "greedy")
        params = dict(solver_base.default_params(solver_name))
        params.update(data.get("params") or {})
        try:
            solver = solver_base.get_solver(solver_name)
            solution = solver.solve(problem, params)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        solution.problem_version = problem.version
        solution.calendar_version = storage.calendar_version(problem_id)
        storage.save_solution(problem_id, solution)
        return jsonify(solution.to_dict()), 201

    @app.route("/api/problems/<problem_id>/solutions", methods=["GET"])
    def solutions(problem_id: str):
        return jsonify({"solutions": storage.list_solutions(problem_id)})

    @app.route("/api/problems/<problem_id>/solutions/<solution_id>", methods=["GET"])
    def solution(problem_id: str, solution_id: str):
        sol = storage.load_solution(problem_id, solution_id)
        if sol is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(sol.to_dict())

    @app.route("/api/problems/<problem_id>/solutions/<solution_id>", methods=["DELETE"])
    def delete_solution(problem_id: str, solution_id: str):
        if storage.delete_solution(problem_id, solution_id):
            return jsonify({"ok": True})
        return jsonify({"error": "not found"}), 404

    # ------------------------------------------------------------------ #
    # Analysis
    # ------------------------------------------------------------------ #
    @app.route("/api/problems/<problem_id>/sensitivity", methods=["POST"])
    def sensitivity_run(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True) or {}
        solver_name = data.get("solver", "greedy")
        spec = data.get("spec", {"kind": "resource_capacity",
                                 "resource": (problem.resources[0].id
                                              if problem.resources else None),
                                 "multipliers": [0.5, 0.75, 1.0, 1.25, 1.5]})
        try:
            result = sensitivity.run_sensitivity(
                problem, solver_name, spec,
                solver_params=data.get("params"),
                persist=bool(data.get("persist", True)))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        result.problem_version = problem.version
        result.calendar_version = storage.calendar_version(problem_id)
        if data.get("persist", True):
            storage.save_sensitivity(problem_id, result)
        return jsonify(result.to_dict()), 201

    @app.route("/api/problems/<problem_id>/sensitivity", methods=["GET"])
    def sensitivity_list(problem_id: str):
        return jsonify({"results": storage.list_sensitivity(problem_id)})

    @app.route("/api/problems/<problem_id>/compare", methods=["POST"])
    def compare(problem_id: str):
        data = request.get_json(force=True) or {}
        ids = data.get("solution_ids", [])
        sols = [storage.load_solution(problem_id, sid) for sid in ids]
        sols = [s for s in sols if s is not None]
        best_obj = min((s.objective_value for s in sols
                        if s.objective_value is not None), default=None)
        rows = []
        for s in sorted(sols, key=lambda s: (s.objective_value is None,
                                             s.objective_value or 0)):
            rows.append({
                **s.to_dict(),
                "delta_vs_best": (round(s.objective_value - best_obj, 4)
                                  if s.objective_value is not None and best_obj is not None
                                  else None),
                "is_best": s.objective_value == best_obj,
            })
        return jsonify({"solutions": rows, "best_objective": best_obj})

    @app.route("/api/problems/<problem_id>/report", methods=["POST"])
    def make_report(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True) or {}
        sols = [storage.load_solution(problem_id, sid)
                for sid in data.get("solution_ids", [])]
        sols = [s for s in sols if s is not None]
        sens = None
        if data.get("sensitivity_id"):
            sens_list = storage.list_sensitivity(problem_id)
            for item in sens_list:
                if item.get("id") == data["sensitivity_id"]:
                    sens = models.SensitivityResult.from_dict(item)
        rep = report.generate_report(problem, sols, sens,
                                     title=data.get("title"),
                                     persist=True)
        rep.problem_version = problem.version
        rep.calendar_version = storage.calendar_version(problem_id)
        storage.save_report(problem_id, rep)
        return jsonify(rep.to_dict()), 201

    @app.route("/api/problems/<problem_id>/reports", methods=["GET"])
    def reports(problem_id: str):
        return jsonify({"reports": storage.list_reports(problem_id)})

    @app.route("/api/problems/<problem_id>/reports/<report_id>", methods=["GET"])
    def get_report(problem_id: str, report_id: str):
        rep = storage.load_report(problem_id, report_id)
        if rep is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(rep.to_dict())

    # ------------------------------------------------------------------ #
    # Shift / calendar templates
    # ------------------------------------------------------------------ #
    @app.route("/api/calendar-presets", methods=["GET"])
    def calendar_presets():
        return jsonify({"presets": cal.preset_templates()})

    @app.route("/api/calendars", methods=["GET"])
    def calendar_list():
        return jsonify({"templates": storage.list_calendar_templates()})

    @app.route("/api/calendars", methods=["POST"])
    def calendar_create():
        data = request.get_json(force=True)
        try:
            template = models.CalendarTemplate.from_dict(data)
            if not template.id:
                template.id = models.new_id("cal_t")
            errors = template.validate()
            if errors:
                return jsonify({"error": "validation failed", "details": errors}), 400
            if storage.load_calendar_template(template.id) is not None:
                return jsonify({"error": "template id already exists"}), 409
            storage.save_calendar_template(template)
            return jsonify(template.to_dict()), 201
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/calendars/<template_id>", methods=["GET"])
    def calendar_get(template_id: str):
        template = storage.load_calendar_template(template_id)
        if template is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(template.to_dict())

    @app.route("/api/calendars/<template_id>", methods=["PUT"])
    def calendar_update(template_id: str):
        if storage.load_calendar_template(template_id) is None:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True)
        try:
            template = models.CalendarTemplate.from_dict(data)
            template.id = template_id
            errors = template.validate()
            if errors:
                return jsonify({"error": "validation failed", "details": errors}), 400
            storage.save_calendar_template(template)
            return jsonify(template.to_dict())
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/calendars/<template_id>", methods=["DELETE"])
    def calendar_delete(template_id: str):
        if storage.delete_calendar_template(template_id):
            return jsonify({"ok": True})
        return jsonify({"error": "not found"}), 404

    @app.route("/api/calendars/<template_id>/preview", methods=["GET"])
    def calendar_preview(template_id: str):
        template = storage.load_calendar_template(template_id)
        if template is None:
            return jsonify({"error": "not found"}), 404
        horizon = request.args.get("horizon", default=168, type=int)
        start_date = request.args.get("start_date") or None
        end_date = request.args.get("end_date") or None
        try:
            intervals = cal.generate_availability(template, horizon,
                                                  start_date, end_date)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"horizon": horizon, "count": len(intervals),
                        "intervals": intervals})

    @app.route("/api/calendars/<template_id>/refresh", methods=["POST"])
    def calendar_refresh(template_id: str):
        """Batch-refresh every assignment using this template, across all
        problems, and report how many derived results went stale."""
        try:
            summary = cal.refresh_template(template_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"refreshed": summary})

    # ------------------------------------------------------------------ #
    # Per-problem calendar application & staleness
    # ------------------------------------------------------------------ #
    @app.route("/api/problems/<problem_id>/calendar", methods=["GET"])
    def calendar_state(problem_id: str):
        if storage.load_problem(problem_id) is None:
            return jsonify({"error": "not found"}), 404
        state = storage.load_calendar_state(problem_id)
        templates = {t["id"]: t for t in storage.list_calendar_templates()}
        assignments = []
        for a in state["assignments"]:
            t = templates.get(a.get("template_id"))
            assignments.append({
                **a,
                "template_name": t["name"] if t else "(模板已删除)",
                "template_current_version": t["version"] if t else None,
                "template_changed": bool(t and t["version"] != a.get("template_version")),
            })
        return jsonify({"version": state["version"], "assignments": assignments})

    @app.route("/api/problems/<problem_id>/calendar/apply", methods=["POST"])
    def calendar_apply(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True) or {}
        template = storage.load_calendar_template(data.get("template_id", ""))
        if template is None:
            return jsonify({"error": "unknown template"}), 400
        resource_ids = data.get("resource_ids") or []
        try:
            assignment, updated = cal.apply_template(
                problem, template, resource_ids,
                start_date=data.get("start_date") or None,
                end_date=data.get("end_date") or None)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({
            "assignment": assignment.to_dict(),
            "resources_updated": updated,
            "problem_version": problem.version,
            "availability_preview": {
                rid: problem.resource_map()[rid].availability for rid in updated
            },
        }), 201

    @app.route("/api/problems/<problem_id>/calendar/assignments/<assignment_id>",
               methods=["DELETE"])
    def calendar_unapply(problem_id: str, assignment_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        if cal.remove_assignment(problem, assignment_id):
            return jsonify({"ok": True,
                            "note": "已解除套用；资源上已生成的可用时段保留，可手工编辑"})
        return jsonify({"error": "not found"}), 404

    @app.route("/api/problems/<problem_id>/staleness", methods=["GET"])
    def problem_staleness(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(cal.staleness(problem))

    # ------------------------------------------------------------------ #
    # Configs
    # ------------------------------------------------------------------ #
    @app.route("/api/problems/<problem_id>/configs", methods=["POST"])
    def save_config(problem_id: str):
        data = request.get_json(force=True) or {}
        cfg = models.SolverConfig(
            id=data.get("id") or models.new_id("cfg"),
            problem_id=problem_id,
            solver=data.get("solver", "greedy"),
            params=data.get("params", {}),
        )
        storage.save_config(problem_id, cfg)
        return jsonify(cfg.to_dict()), 201

    @app.route("/api/problems/<problem_id>/configs", methods=["GET"])
    def configs(problem_id: str):
        return jsonify({"configs": storage.list_configs(problem_id)})

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
