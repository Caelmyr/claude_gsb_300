"""Seed example problem instances so the UI is populated on first run."""

from __future__ import annotations

import os
from typing import List

from . import calendar as cal_engine
from . import models, storage


# 2024-01-01 is a Monday -- convenient for weekly calendars.
SEED_START_DATE = "2024-01-01"


def _jobshop() -> models.Problem:
    p = models.Problem(
        id="demo_jobshop",
        name="作业车间示例",
        description="3 个作业在 2 台机器上加工，含先后顺序链，目标为最小化完工时间。",
        horizon=40,
        time_unit="小时",
        start_date=SEED_START_DATE,
        slots_per_day=24,
        resources=[
            models.Resource(id="M1", name="机器 A", type="equipment", capacity=1,
                            cost_per_unit=5.0),
            models.Resource(id="M2", name="机器 B", type="equipment", capacity=1,
                            cost_per_unit=8.0),
        ],
        tasks=[
            models.Task(id="J1A", name="作业1 工序A", duration=4,
                        resource_requirements={"M1": 1}),
            models.Task(id="J1B", name="作业1 工序B", duration=3,
                        resource_requirements={"M2": 1}, dependencies=["J1A"]),
            models.Task(id="J2A", name="作业2 工序A", duration=5,
                        resource_requirements={"M2": 1}),
            models.Task(id="J2B", name="作业2 工序B", duration=2,
                        resource_requirements={"M1": 1}, dependencies=["J2A"]),
            models.Task(id="J3A", name="作业3 工序A", duration=3,
                        resource_requirements={"M1": 1}),
            models.Task(id="J3B", name="作业3 工序B", duration=4,
                        resource_requirements={"M2": 1}, dependencies=["J3A"]),
        ],
        objective=models.Objective(type="makespan"),
    )
    return p


def _staffing() -> models.Problem:
    # ---- templates: 白班 / 中班 / 夜班, each 8h, covering the 24h day ----
    shifts = [
        models.ShiftTemplate(id="day", name="白班", color="#59a14f",
                             segments=[[8, 16]]),
        models.ShiftTemplate(id="swing", name="中班", color="#edc949",
                             segments=[[16, 24]]),
        models.ShiftTemplate(id="night", name="夜班", color="#4e79a7",
                             segments=[[0, 8]]),
        models.ShiftTemplate(id="allday", name="全天班", color="#86bcb6",
                             segments=[[0, 24]]),
    ]
    weekend = {str(i): [] for i in (5, 6)}
    sunday_closed = {"6": []}

    # 三班倒：每天三个班次都有人（资源级绑定决定谁上哪个班）。
    cal_rotating = models.CalendarTemplate(
        id="cal_rotating", name="三班倒（含周末）",
        description="白班 + 中班 + 夜班轮班，周末照常。",
        weekday_shifts={str(i): ["day", "swing", "night"] for i in range(7)},
    )
    # 全天可用的场地，仅元旦关闭。
    cal_room = models.CalendarTemplate(
        id="cal_room", name="场地日历",
        description="全天可用，法定节假日关闭。",
        weekday_shifts={str(i): ["allday"] for i in range(7)},
        holidays={"2024-01-01": "closed"},
    )
    # 设备日历：周一至周六可用，周日停机保养（节假日照常关闭）。
    cal_machine = models.CalendarTemplate(
        id="cal_machine", name="设备（周日保养）",
        description="周一至周六全天可用，周日定期保养停机。",
        weekday_shifts={**{str(i): ["allday"] for i in range(6)}, **sunday_closed},
        holidays={"2024-01-01": "closed"},
    )
    # 常白班：周一至周五白班，周末休息（供临时绑定/演示用）。
    cal_dayweek = models.CalendarTemplate(
        id="cal_dayweek", name="常白班（双休）",
        description="周一至周五白班，周六周日休息。",
        weekday_shifts={**{str(i): ["day"] for i in range(5)}, **weekend},
        holidays={"2024-01-01": "closed"},   # 元旦
    )

    p = models.Problem(
        id="demo_staffing",
        name="班次排班示例",
        description="白班/中班/夜班三班倒，含周末双休、节假日与周日设备保养；目标为加权完工时间。",
        horizon=216,
        time_unit="小时",
        start_date=SEED_START_DATE,
        slots_per_day=24,
        shifts=shifts,
        calendars=[cal_rotating, cal_room, cal_machine, cal_dayweek],
        resources=[
            models.Resource(id="P1", name="护士甲（三班倒）", type="personnel", capacity=1,
                            skills=["护理"], cost_per_unit=12.0),
            models.Resource(id="P2", name="护士乙（三班倒）", type="personnel", capacity=1,
                            skills=["护理", "转运"], cost_per_unit=10.0),
            models.Resource(id="P3", name="护士丙（三班倒）", type="personnel", capacity=1,
                            skills=["护理"], cost_per_unit=11.0),
            models.Resource(id="R1", name="检查室", type="equipment", capacity=2,
                            cost_per_unit=2.0),
            models.Resource(id="E1", name="超声设备（周日保养）", type="equipment", capacity=1,
                            cost_per_unit=4.0),
            models.Resource(id="T1", name="班次时段", type="time", capacity=3),
        ],
        bindings=[
            models.CalendarBinding(id="b_p1", resource_id="P1",
                                   calendar_id="cal_rotating"),
            models.CalendarBinding(id="b_p2", resource_id="P2",
                                   calendar_id="cal_rotating"),
            models.CalendarBinding(id="b_p3", resource_id="P3",
                                   calendar_id="cal_rotating"),
            models.CalendarBinding(id="b_r1", resource_id="R1",
                                   calendar_id="cal_room"),
            models.CalendarBinding(id="b_e1", resource_id="E1",
                                   calendar_id="cal_machine"),
            models.CalendarBinding(id="b_t1", resource_id="T1",
                                   calendar_id="cal_rotating"),
        ],
        tasks=[
            models.Task(id="A1", name="入院1", duration=6,
                        resource_requirements={"P1": 1, "R1": 1, "T1": 1},
                        release_time=24, due_date=40, weight=2),
            models.Task(id="A2", name="入院2", duration=6,
                        resource_requirements={"P2": 1, "R1": 1, "T1": 1},
                        release_time=24, due_date=42, weight=2),
            models.Task(id="A3", name="入院3", duration=6,
                        resource_requirements={"P3": 1, "R1": 1, "T1": 1},
                        release_time=24, due_date=44, weight=2),
            models.Task(id="B1", name="治疗1", duration=8,
                        resource_requirements={"P1": 1, "T1": 1},
                        dependencies=["A1"], due_date=52, weight=1),
            models.Task(id="B2", name="治疗2", duration=8,
                        resource_requirements={"P2": 1, "T1": 1},
                        dependencies=["A2"], due_date=54, weight=1),
            models.Task(id="B3", name="治疗3", duration=8,
                        resource_requirements={"P3": 1, "T1": 1},
                        dependencies=["A3"], due_date=56, weight=1),
            models.Task(id="C1", name="转运", duration=3,
                        resource_requirements={"P2": 1, "E1": 1},
                        dependencies=["B1", "B2"], due_date=60, weight=3),
        ],
        hard_constraints=[
            models.HardConstraint(id="hc1", type="time_window",
                                 params={"task": "C1", "release": 15}),
            models.HardConstraint(id="hc2", type="max_concurrent",
                                 params={"limit": 2}),
        ],
        soft_constraints=[
            models.SoftConstraint(id="sc1", type="due_date",
                                  params={"task": "C1"}, penalty=5.0),
            models.SoftConstraint(id="sc2", type="preferred_window",
                                  params={"task": "B2", "start": 10, "end": 20},
                                  penalty=0.5),
        ],
        objective=models.Objective(type="weighted_completion"),
    )
    # Generate the initial availability intervals from the templates so the
    # instance is immediately solvable and provenance starts life consistent.
    cal_engine.refresh_all(p)
    return p


def _large() -> models.Problem:
    """A larger instance meant to showcase GA/SA on the big end."""
    resources = [
        models.Resource(id=f"M{i}", name=f"机器 {i}", type="equipment",
                        capacity=1, cost_per_unit=float(i))
        for i in range(1, 5)
    ]
    tasks: List[models.Task] = []
    for j in range(1, 26):
        n_ops = 3
        prev = None
        for k in range(n_ops):
            tid = f"J{j}O{k}"
            deps = [prev] if prev else []
            tasks.append(models.Task(
                id=tid, name=f"作业 {j} 工序 {k}",
                duration=1 + ((j + k) % 6),
                resource_requirements={f"M{1 + (j + k) % 4}": 1},
                dependencies=deps,
                release_time=0,
                due_date=12 + j + k,
                weight=1 + (j % 3),
            ))
            prev = tid
    return models.Problem(
        id="demo_large",
        name="大型作业车间（25 个作业）",
        description="75 道工序、4 台机器；面向元启发式算法。",
        horizon=200,
        time_unit="分钟",
        resources=resources,
        tasks=tasks,
        objective=models.Objective(type="makespan"),
    )


def seed_all(force: bool = False) -> List[str]:
    """Create the example instances if they do not already exist.

    With ``force`` the demo instances are rebuilt from scratch, including
    deleting previously generated (and therefore possibly stale) solutions,
    sensitivity runs and reports so the shipped data starts self-consistent."""
    created: List[str] = []
    for builder in (_jobshop, _staffing, _large):
        problem = builder()
        existed = storage.load_problem(problem.id) is not None
        if not force and existed:
            continue
        if force and existed:
            import shutil
            for sub in ("solutions", "sensitivity", "reports", "configs", "versions"):
                d = os.path.join(storage.instance_dir(problem.id), sub)
                if os.path.isdir(d):
                    shutil.rmtree(d)
        storage.save_problem(problem)
        created.append(problem.id)
    return created


if __name__ == "__main__":
    print("seeded:", seed_all())
