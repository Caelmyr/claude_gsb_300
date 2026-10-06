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

from . import calendar, models, report, sensitivity, storage
from . import freshness
from .solvers import base as solver_base

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "frontend")


def create_app() -> Flask:
    app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="/static")
    storage.ensure_dirs()

    def _mark_manual_availability(problem_id: str, problem) -> None:
        """Tag resources whose availability list was edited outside the
        calendar engine, so the refresh flow can warn before overwriting."""
        old = storage.load_problem(problem_id)
        old_map = old.resource_map() if old else {}
        for r in problem.resources:
            prev = old_map.get(r.id)
            if prev is None:
                # brand-new resource: intervals authored by hand
                if r.availability is not None and (r.availability_meta or {}).get("source") != "calendar":
                    r.availability_meta = {"source": "manual",
                                           "generated_at": models.now_iso()}
                continue
            if (r.availability_meta or {}).get("source") == "calendar":
                continue
            if r.availability != prev.availability:
                r.availability_meta = {
                    "source": "manual",
                    "note": "hand-edited; a calendar refresh will overwrite",
                    "generated_at": models.now_iso(),
                }

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
            _mark_manual_availability(problem_id, problem)
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
        storage.save_solution(problem_id, solution)
        return jsonify(solution.to_dict()), 201

    @app.route("/api/problems/<problem_id>/solutions", methods=["GET"])
    def solutions(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        rows = [freshness.evaluate_solution(
            problem, storage.load_solution(problem_id, row["id"]))
            for row in storage.list_solutions(problem_id)
            if storage.load_solution(problem_id, row["id"]) is not None]
        return jsonify({"solutions": rows})

    @app.route("/api/problems/<problem_id>/solutions/<solution_id>", methods=["GET"])
    def solution(problem_id: str, solution_id: str):
        sol = storage.load_solution(problem_id, solution_id)
        if sol is None:
            return jsonify({"error": "not found"}), 404
        problem = storage.load_problem(problem_id)
        data = sol.to_dict()
        if problem is not None:
            data["freshness"] = freshness.evaluate_solution(problem, sol)
        return jsonify(data)

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
        return jsonify(result.to_dict()), 201

    @app.route("/api/problems/<problem_id>/sensitivity", methods=["GET"])
    def sensitivity_list(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        results = [freshness.evaluate_sensitivity(
            problem, models.SensitivityResult.from_dict(item))
            for item in storage.list_sensitivity(problem_id)]
        return jsonify({"results": results})

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
                                     persist=True,
                                     solution_ids=data.get("solution_ids"))
        return jsonify(rep.to_dict()), 201

    @app.route("/api/problems/<problem_id>/reports", methods=["GET"])
    def reports(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        summary = freshness.freshness_summary(problem)
        sol_states = {s["id"]: s for s in summary["solutions"]}
        sens_states = {s["id"]: s for s in summary["sensitivity"]}
        rows = [freshness.evaluate_report(
            problem, models.Report.from_dict(item), sol_states, sens_states)
            for item in storage.list_reports(problem_id)]
        return jsonify({"reports": rows})

    @app.route("/api/problems/<problem_id>/reports/<report_id>", methods=["GET"])
    def get_report(problem_id: str, report_id: str):
        rep = storage.load_report(problem_id, report_id)
        if rep is None:
            return jsonify({"error": "not found"}), 404
        problem = storage.load_problem(problem_id)
        data = rep.to_dict()
        if problem is not None:
            summary = freshness.freshness_summary(problem)
            sol_states = {s["id"]: s for s in summary["solutions"]}
            sens_states = {s["id"]: s for s in summary["sensitivity"]}
            data["freshness"] = freshness.evaluate_report(
                problem, rep, sol_states, sens_states)
        return jsonify(data)

    # ------------------------------------------------------------------ #
    # Calendar / shift templates
    # ------------------------------------------------------------------ #
    def _calendar_payload(problem) -> Dict[str, Any]:
        """Resource rows augmented with binding/availability provenance."""
        resources = []
        for r in problem.resources:
            bindings = [b.to_dict() for b in problem.bindings_for(r.id)]
            resources.append({
                **r.to_dict(),
                "bindings": bindings,
            })
        return {
            "start_date": problem.start_date,
            "slots_per_day": problem.slots_per_day,
            "horizon": problem.horizon,
            "weekdays": [{"index": i, "name": models.WEEKDAY_NAMES[i],
                          "label": models.WEEKDAY_LABELS[i]} for i in range(7)],
            "shifts": [s.to_dict() for s in problem.shifts],
            "calendars": [c.to_dict() for c in problem.calendars],
            "bindings": [b.to_dict() for b in problem.bindings],
            "resources": resources,
        }

    @app.route("/api/problems/<problem_id>/calendar", methods=["GET"])
    def calendar_get(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(_calendar_payload(problem))

    @app.route("/api/problems/<problem_id>/calendar", methods=["PUT"])
    def calendar_save(problem_id: str):
        """Save shifts / calendars / bindings (and optional date anchor).

        Template edits alone never touch generated availability; the response
        lists resources whose stored intervals are now out of sync so the UI
        can offer a refresh."""
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(force=True) or {}
        try:
            if "start_date" in data:
                problem.start_date = data["start_date"]
            if "slots_per_day" in data:
                problem.slots_per_day = int(data["slots_per_day"])
            if "shifts" in data:
                problem.shifts = [models.ShiftTemplate.from_dict(s)
                                  for s in data["shifts"]]
            if "calendars" in data:
                problem.calendars = [models.CalendarTemplate.from_dict(c)
                                     for c in data["calendars"]]
            if "bindings" in data:
                problem.bindings = [models.CalendarBinding.from_dict(b)
                                    for b in data["bindings"]]
            errors = models.validate_problem(problem)
            if errors:
                return jsonify({"error": "validation failed", "details": errors}), 400
            storage.save_problem(problem)
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

        preview = calendar.preview_refresh(problem)
        pending = [row for row in preview if row["changed"]]
        return jsonify({
            **_calendar_payload(problem),
            "problem_version": problem.version,
            "pending_refresh": pending,
        })

    @app.route("/api/problems/<problem_id>/calendar/preview", methods=["POST"])
    def calendar_preview(problem_id: str):
        """Dry-run expansion. Accepts optional full calendar payload (so the UI
        can preview unsaved edits), otherwise expands the saved problem."""
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(silent=True) or {}
        if any(k in data for k in ("shifts", "calendars", "bindings")):
            problem = models.Problem.from_dict(problem.to_dict())
            if "shifts" in data:
                problem.shifts = [models.ShiftTemplate.from_dict(s)
                                  for s in data["shifts"]]
            if "calendars" in data:
                problem.calendars = [models.CalendarTemplate.from_dict(c)
                                     for c in data["calendars"]]
            if "bindings" in data:
                problem.bindings = [models.CalendarBinding.from_dict(b)
                                    for b in data["bindings"]]
            if "start_date" in data:
                problem.start_date = data["start_date"]
            if "slots_per_day" in data:
                problem.slots_per_day = int(data["slots_per_day"])
            errors = models.validate_problem(problem)
            if errors:
                return jsonify({"error": "validation failed", "details": errors}), 400
        ids = data.get("resource_ids")
        rows = calendar.preview_refresh(problem, ids)
        return jsonify({"rows": rows})

    @app.route("/api/problems/<problem_id>/calendar/refresh", methods=["POST"])
    def calendar_refresh(problem_id: str):
        """Batch-regenerate availability from templates and persist a new
        problem version. The response includes the post-refresh freshness
        summary so the caller can immediately see which results went stale."""
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(silent=True) or {}
        ids = data.get("resource_ids")
        result = calendar.refresh_all(problem, ids)
        storage.save_problem(problem)
        result["problem_version"] = problem.version
        result["freshness"] = freshness.freshness_summary(problem)
        return jsonify(result)

    @app.route("/api/problems/<problem_id>/freshness", methods=["GET"])
    def freshness_get(problem_id: str):
        problem = storage.load_problem(problem_id)
        if problem is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(freshness.freshness_summary(problem))

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
