"""
Report generation.

Produces a self-contained Markdown report for a problem instance, optionally
including one or more solutions, a comparison table and a sensitivity summary.
The report is stored alongside the instance (see :mod:`storage`) so it survives
as a historical artefact and can be viewed from the "report" page.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import models, storage


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def solution_markdown(sol: models.Solution) -> str:
    lines = [
        f"### 方案 {sol.id}",
        "",
        f"- **求解器**: `{sol.solver}`  ·  **状态**: {sol.status}  ·  "
        f"**目标值**: {_fmt(sol.objective_value)}  ·  **完工时间**: {_fmt(sol.makespan)}",
        f"- **耗时**: {_fmt(sol.solve_time)} s  ·  "
        f"**下界**: {_fmt(sol.lower_bound)}",
    ]
    m = sol.metrics
    if m:
        lines.append(f"- **指标**: 完工时间={_fmt(m.get('makespan'))}, "
                     f"总拖期={_fmt(m.get('total_tardiness'))}, "
                     f"资源成本={_fmt(m.get('resource_cost'))}, "
                     f"违反数={_fmt(m.get('n_violations'))}")
    if sol.message:
        lines.append(f"- **说明**: {sol.message}")
    if sol.assignments:
        lines.append("")
        lines.append("| 任务 | 开始 | 结束 | 资源 |")
        lines.append("|------|-------|-----|-----------|")
        for a in sorted(sol.assignments, key=lambda a: (a.start, a.task)):
            lines.append(f"| {a.task} | {a.start} | {a.end} | {', '.join(a.resources)} |")
    lines.append("")
    return "\n".join(lines)


def comparison_markdown(solutions: List[models.Solution]) -> str:
    lines = [
        "## 方案对比",
        "",
        "| 求解器 | 状态 | 目标值 | 完工时间 | 耗时(s) | 下界 |",
        "|--------|--------|-----------|----------|----------|-------------|",
    ]
    for s in sorted(solutions, key=lambda s: (s.objective_value is None, s.objective_value or 0)):
        lines.append(f"| {s.solver} | {s.status} | {_fmt(s.objective_value)} | "
                     f"{_fmt(s.makespan)} | {_fmt(s.solve_time)} | {_fmt(s.lower_bound)} |")
    lines.append("")
    return "\n".join(lines)


def sensitivity_markdown(result: models.SensitivityResult) -> str:
    lines = [
        "## 敏感性分析",
        "",
        f"- 参数: `{result.parameter}`  ·  求解器 `{result.solver}`  ·  "
        f"基准目标值 {_fmt(result.base_objective)}",
        "",
        "| 变量取值 | 目标值 | 完工时间 | 目标值变化 | 状态 |",
        "|-----------|-----------|----------|-------------|--------|",
    ]
    for v in result.variations:
        lines.append(f"| {v.get('label')} | {_fmt(v.get('objective_value'))} | "
                     f"{_fmt(v.get('makespan'))} | {_fmt(v.get('delta'))} | {v.get('status')} |")
    lines.append("")
    return "\n".join(lines)


def _stale_warning(problem: models.Problem,
                   solutions: List[models.Solution],
                   sensitivity: Optional[models.SensitivityResult]) -> List[str]:
    """Return report lines warning about artefacts already stale at generation
    time, so the warning travels *inside* an exported/printed report too."""
    from . import freshness
    lines: List[str] = []
    bad = []
    for s in solutions:
        info = freshness.evaluate_solution(problem, s)
        if info["stale"]:
            bad.append(f"{s.id}（{'；'.join(info['stale_reasons'][:2])}）")
    if bad:
        lines.append("> ⚠️ **以下方案基于旧班次/输入，结果可能已失效，请重排后再执行：** "
                     + "、".join(bad))
        lines.append("")
    if sensitivity is not None:
        info = freshness.evaluate_sensitivity(
            problem, sensitivity)
        if info["stale"]:
            lines.append("> ⚠️ **本报告引用的敏感性分析已失效：** "
                         + "；".join(info["stale_reasons"][:2]))
            lines.append("")
    return lines


def generate_report(problem: models.Problem,
                    solutions: Optional[List[models.Solution]] = None,
                    sensitivity: Optional[models.SensitivityResult] = None,
                    title: Optional[str] = None,
                    persist: bool = True,
                    solution_ids: Optional[List[str]] = None) -> models.Report:
    solutions = solutions or []
    lines = [
        f"# 排程报告：{problem.name or problem.id}",
        "",
        f"- **实例**: `{problem.id}`  ·  **版本**: {problem.version}",
        f"- **任务数**: {len(problem.tasks)}  ·  **资源数**: {len(problem.resources)}"
        f"  ·  **计划周期**: {problem.horizon} {problem.time_unit}",
        f"- **目标**: {models.objective_summary(problem)}",
        f"- **生成时间**: {models.now_iso()}",
        "",
    ]
    lines += _stale_warning(problem, solutions, sensitivity)
    lines += [
        "## 资源",
        "",
        "| ID | 名称 | 类型 | 容量 | 单位成本 |",
        "|----|------|------|----------|-----------|",
    ]
    for r in problem.resources:
        lines.append(f"| {r.id} | {r.name} | {r.type} | {_fmt(r.capacity)} | {_fmt(r.cost_per_unit)} |")

    lines.append("")
    lines.append("## 任务")
    lines.append("")
    lines.append("| ID | 名称 | 工期 | 最早开始 | 截止 | 依赖 | 资源 |")
    lines.append("|----|------|----------|---------|-----|--------------|-----------|")
    for t in problem.tasks:
        deps = ",".join(t.dependencies) or "-"
        res = ",".join(f"{k}:{v}" for k, v in t.resource_requirements.items()) or "-"
        lines.append(f"| {t.id} | {t.name} | {t.duration} | {t.release_time} | "
                     f"{_fmt(t.due_date)} | {deps} | {res} |")

    lines.append("")
    if solutions:
        lines.append("## 求解方案")
        lines.append("")
        for sol in solutions:
            lines.append(solution_markdown(sol))
    if len(solutions) > 1:
        lines.append(comparison_markdown(solutions))
    if sensitivity is not None:
        lines.append(sensitivity_markdown(sensitivity))

    content = "\n".join(lines)
    report = models.Report(
        id=models.new_id("rep"),
        problem_id=problem.id,
        title=title or f"{problem.name} 报告",
        format="markdown",
        content=content,
    )
    ids = solution_ids if solution_ids is not None else [s.id for s in solutions]
    from . import freshness
    freshness.stamp_report(problem, report, solution_ids=ids,
                           sensitivity_id=sensitivity.id if sensitivity else None)
    if persist:
        storage.save_report(problem.id, report)
    return report
