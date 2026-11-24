"""Tareas periódicas: RRULE básico, siguiente fecha y registro de ejecuciones."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from gtd.db import exec_sql, q1
from gtd.kanban_board import _project_board_status_for_task


@dataclass
class RRule:
    freq: str
    interval: int = 1
    byday: Optional[List[str]] = None
    bymonthday: Optional[int] = None

def parse_rrule(rrule: str) -> Optional[RRule]:
    if not rrule:
        return None
    parts: Dict[str, str] = {}
    for item in rrule.split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip().upper()] = v.strip().upper()
    freq = parts.get("FREQ")
    if not freq:
        return None
    interval_raw = parts.get("INTERVAL", "1") or "1"
    try:
        interval = int(interval_raw)
    except (TypeError, ValueError):
        interval = 1
    if interval < 1:
        interval = 1
    byday = [d.strip() for d in parts.get("BYDAY", "").split(",") if d.strip()] or None
    bymonthday = None
    if parts.get("BYMONTHDAY"):
        try:
            bymonthday = int(parts["BYMONTHDAY"])
        except ValueError:
            bymonthday = None
    return RRule(freq=freq, interval=interval, byday=byday, bymonthday=bymonthday)

WEEKDAY_MAP = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

def next_due_date(current_due: date, rule: RRule) -> date:
    if rule.freq == "DAILY":
        return current_due + timedelta(days=rule.interval)

    if rule.freq == "WORKDAY":
        cand = current_due
        remaining = max(rule.interval, 1)
        while remaining > 0:
            cand = cand + timedelta(days=1)
            if cand.weekday() < 5:
                remaining -= 1
        return cand

    if rule.freq == "WEEKLY":
        if rule.byday:
            targets = sorted(WEEKDAY_MAP[d] for d in rule.byday if d in WEEKDAY_MAP)
            if targets:
                for delta in range(1, 15):
                    cand = current_due + timedelta(days=delta)
                    if cand.weekday() in targets:
                        return cand
        return current_due + timedelta(weeks=rule.interval)

    if rule.freq == "MONTHLY":
        y, m = current_due.year, current_due.month
        m += rule.interval
        while m > 12:
            y += 1
            m -= 12
        day = rule.bymonthday if rule.bymonthday else min(current_due.day, 28)
        for d in range(day, 0, -1):
            try:
                return date(y, m, d)
            except ValueError:
                continue
        return date(y, m, 1)

    if rule.freq == "YEARLY":
      y = current_due.year + rule.interval
      m = current_due.month
      d = current_due.day

      # Manejo seguro de 29 Feb, etc.
      while True:
        try:
            return date(y, m, d)
        except ValueError:
            d -= 1
            if d < 1:
                return date(y, m, 1)

    return current_due + timedelta(weeks=1)


def next_due_date_after_today(current_due: date, rule: RRule, today_d: date) -> date:
    """Return the first recurrence date on or after today."""
    due = next_due_date(current_due, rule)
    guard = 0
    while due < today_d and guard < 512:
        nxt = next_due_date(due, rule)
        if nxt <= due:
            break
        due = nxt
        guard += 1
    return due


def recurrence_due_options(base_due: date, rule: RRule, today_d: date) -> Dict[str, Any]:
    """Compute recurring due options based on previous due date and current day.

    Rules:
    - Always compute +1 period from previous due date.
    - If +1 period is today/future, use it directly.
    - If +1 period is still in the past, allow choosing between:
      A) +1 period
      B) first recurrence strictly after today.
    """
    # Si la fecha base es hoy o futura, nunca hay ambigüedad: se avanza 1 período.
    if base_due >= today_d:
        next_due_once = next_due_date(base_due, rule)
        return {
            "requires_choice": False,
            "option_a_due": next_due_once,
            "option_b_due": next_due_once,
            "default_due": next_due_once,
        }

    next_due_once = next_due_date(base_due, rule)

    if next_due_once >= today_d:
        return {
            "requires_choice": False,
            "option_a_due": next_due_once,
            "option_b_due": next_due_once,
            "default_due": next_due_once,
        }

    option_b_due = next_due_once
    guard = 0
    while option_b_due <= today_d and guard < 512:
        nxt = next_due_date(option_b_due, rule)
        if nxt <= option_b_due:
            break
        option_b_due = nxt
        guard += 1

    return {
        "requires_choice": True,
        "option_a_due": next_due_once,
        "option_b_due": option_b_due,
        "default_due": next_due_once,
    }


def resolve_recurrence_due_choice(
    base_due: date,
    rule: RRule,
    today_d: date,
    choice_raw: Optional[str] = None,
) -> Dict[str, Any]:
    options = recurrence_due_options(base_due, rule, today_d)
    choice = (choice_raw or "").strip().lower()

    selected_due = options["default_due"]
    if options["requires_choice"] and choice in {"future", "option_b"}:
        selected_due = options["option_b_due"]

    options["selected_due"] = selected_due
    return options


def effective_recurrence_base_due(task_row: Dict[str, Any]) -> Optional[date]:
    """Return the most recent due date baseline for recurring tasks."""
    base_due = task_row.get("due_date")
    last_next_due = task_row.get("last_next_due_date")

    if base_due is None:
        return last_next_due
    if last_next_due is None:
        return base_due
    return last_next_due if last_next_due > base_due else base_due


def complete_recurring_task_run(
    task_id: int,
    task: Dict[str, Any],
    now: datetime,
    recurrence_due_choice: Optional[str] = None,
) -> bool:
    """Registra la ejecución de una tarea periódica.

    Si la tarea tiene `max_occurrences` y esta ejecución alcanza el límite,
    detiene la recurrencia (quita recurrence_rule y marca la tarea como completada)
    en lugar de calcular la siguiente fecha. Devuelve True si se detuvo la recurrencia.
    """
    rule = parse_rrule(task["recurrence_rule"])
    base_due = effective_recurrence_base_due(task)
    previous_due = base_due
    today_d = now.date()
    due_resolution = resolve_recurrence_due_choice(base_due, rule, today_d, recurrence_due_choice)
    next_due = due_resolution["selected_due"]

    max_occurrences = task.get("max_occurrences")
    stop_recurrence = False
    if max_occurrences:
        executed_row = q1(
            "SELECT COUNT(*) AS c FROM recurring_task_runs WHERE task_id=%s",
            (task_id,),
        )
        already_executed = int(executed_row["c"]) if executed_row else 0
        if already_executed + 1 >= int(max_occurrences):
            stop_recurrence = True

    if stop_recurrence:
        exec_sql(
            "UPDATE tasks SET last_completed_at=%s, completed_at=%s, recurrence_rule=NULL WHERE id=%s",
            (now, now, task_id),
        )
        project_row = q1("SELECT project_id FROM tasks WHERE id=%s", (task_id,))
        _project_board_status_for_task(task_id, project_row.get("project_id") if project_row else None, "done")
        exec_sql(
            "INSERT INTO recurring_task_runs(task_id, executed_at, previous_due_date, next_due_date) "
            "VALUES(%s, %s, %s, %s)",
            (task_id, now, previous_due, None),
        )
    else:
        exec_sql(
            "UPDATE tasks SET last_completed_at=%s, due_date=%s, completed_at=NULL WHERE id=%s",
            (now, next_due, task_id),
        )
        exec_sql(
            "INSERT INTO recurring_task_runs(task_id, executed_at, previous_due_date, next_due_date) "
            "VALUES(%s, %s, %s, %s)",
            (task_id, now, previous_due, next_due),
        )

    exec_sql("UPDATE subtasks SET completed_at=NULL WHERE task_id=%s", (task_id,))
    return stop_recurrence


def recurring_effective_due_sql(task_alias: str = "t") -> Tuple[str, str]:
    recurring_due_join = (
        "LEFT JOIN ("
        "SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs "
        "GROUP BY task_id"
        ") rr ON rr.task_id=" + task_alias + ".id "
    )
    effective_due_expr = (
        "CASE "
        f"WHEN {task_alias}.recurrence_rule IS NOT NULL AND TRIM({task_alias}.recurrence_rule) <> '' "
        "AND rr.last_next_due_date IS NOT NULL "
        f"AND ({task_alias}.due_date IS NULL OR rr.last_next_due_date > {task_alias}.due_date) "
        "THEN rr.last_next_due_date "
        f"ELSE {task_alias}.due_date "
        "END"
    )
    return recurring_due_join, effective_due_expr
