"""Creación/actualización del esquema de BD al arrancar."""
from __future__ import annotations

from browser_push import PUSH_SCHEMA
from reminders import REMINDER_SCHEMA

from gtd.db import commit, exec_sql, q, q1


_schema_bootstrapped = False


def ensure_schema_updates() -> None:
    global _schema_bootstrapped
    if _schema_bootstrapped:
        return

    for statement in REMINDER_SCHEMA:
        exec_sql(statement)
    exec_sql("ALTER TABLE task_reminders ADD COLUMN IF NOT EXISTS send_telegram TINYINT(1) NOT NULL DEFAULT 1")
    exec_sql("ALTER TABLE task_reminders ADD COLUMN IF NOT EXISTS send_browser TINYINT(1) NOT NULL DEFAULT 0")
    for statement in PUSH_SCHEMA:
        exec_sql(statement)
    exec_sql("ALTER TABLE push_subscriptions ADD COLUMN IF NOT EXISTS expires_at_utc DATETIME NULL AFTER active")
    exec_sql(
        "CREATE TABLE IF NOT EXISTS tag_locations ("
        "tag_id INT NOT NULL PRIMARY KEY, latitude DOUBLE NOT NULL, "
        "longitude DOUBLE NOT NULL, radius_m DOUBLE NOT NULL, "
        "CONSTRAINT fk_tag_location_tag FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    exec_sql(
        "CREATE TABLE IF NOT EXISTS notification_reads ("
        "reminder_id INT NOT NULL, scheduled_at_utc DATETIME NOT NULL, read_at_utc DATETIME NOT NULL, "
        "PRIMARY KEY (reminder_id, scheduled_at_utc), "
        "CONSTRAINT fk_notification_read_reminder FOREIGN KEY (reminder_id) "
        "REFERENCES task_reminders(id) ON DELETE CASCADE"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )

    # Normalize tag types while preserving values from installations that still
    # have the original free-text `tags.type` column.
    exec_sql(
        "CREATE TABLE IF NOT EXISTS TagsType ("
        "id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
        "name VARCHAR(80) NOT NULL, UNIQUE KEY uq_tags_type_name (name)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    exec_sql("ALTER TABLE tags ADD COLUMN IF NOT EXISTS type_id INT NULL AFTER name")
    tag_columns = q("SHOW COLUMNS FROM tags") or []
    tag_column_names = {row.get("Field") for row in tag_columns}
    if "type" in tag_column_names:
        exec_sql(
            "INSERT IGNORE INTO TagsType(name) "
            "SELECT DISTINCT TRIM(type) FROM tags "
            "WHERE type IS NOT NULL AND TRIM(type) <> ''"
        )
        exec_sql(
            "UPDATE tags tg JOIN TagsType tt ON tt.name=TRIM(tg.type) "
            "SET tg.type_id=tt.id WHERE tg.type_id IS NULL"
        )
        exec_sql("ALTER TABLE tags DROP COLUMN type")
    type_fk = q1(
        "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tags' "
        "AND COLUMN_NAME='type_id' AND REFERENCED_TABLE_NAME='TagsType' LIMIT 1"
    )
    if not type_fk:
        exec_sql(
            "ALTER TABLE tags ADD CONSTRAINT fk_tags_type "
            "FOREIGN KEY (type_id) REFERENCES TagsType(id) "
            "ON DELETE SET NULL ON UPDATE CASCADE"
        )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS due_time TIME NULL AFTER due_date"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS location VARCHAR(1024) NULL AFTER notes"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS archived TINYINT(1) NOT NULL DEFAULT 0 AFTER recurrence_rule"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS archived_at DATETIME NULL AFTER archived"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS deleted_at DATETIME NULL AFTER archived_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS deleted_prev_archived TINYINT(1) NOT NULL DEFAULT 0 AFTER deleted_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS priority TINYINT NULL DEFAULT NULL AFTER recurrence_rule"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "MODIFY COLUMN priority TINYINT NULL DEFAULT NULL"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS kanban_status VARCHAR(20) NOT NULL DEFAULT 'inbox' AFTER sort_order"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS project_board_status VARCHAR(80) NULL AFTER kanban_status"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS project_board_order INT NULL AFTER project_board_status"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS archived_at DATETIME NULL AFTER archived"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS deleted_at DATETIME NULL AFTER archived_at"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS deleted_prev_archived TINYINT(1) NOT NULL DEFAULT 0 AFTER deleted_at"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS board_columns JSON NULL AFTER board_visible"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_archived (archived)"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_archived_at (archived_at)"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_deleted_at (deleted_at)"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_priority (priority)"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD INDEX IF NOT EXISTS idx_projects_archived_at (archived_at)"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD INDEX IF NOT EXISTS idx_projects_deleted_at (deleted_at)"
    )

    # Calendar sync metadata (bidireccional)
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS google_calendar_id VARCHAR(255) NULL AFTER archived_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS google_event_id VARCHAR(255) NULL AFTER google_calendar_id"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS google_event_etag VARCHAR(255) NULL AFTER google_event_id"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_sync_state VARCHAR(40) NOT NULL DEFAULT 'none' AFTER google_event_etag"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_sync_ignored TINYINT(1) NOT NULL DEFAULT 0 AFTER calendar_sync_state"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_sync_error TEXT NULL AFTER calendar_sync_state"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_last_synced_hash CHAR(64) NULL AFTER calendar_sync_error"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_last_synced_at DATETIME NULL AFTER calendar_last_synced_hash"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_local_changed_at DATETIME NULL AFTER calendar_last_synced_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_remote_updated_at DATETIME NULL AFTER calendar_local_changed_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_conflict_payload LONGTEXT NULL AFTER calendar_remote_updated_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_conflict_at DATETIME NULL AFTER calendar_conflict_payload"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_google_event_id (google_event_id)"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_calendar_sync_state (calendar_sync_state)"
    )

    exec_sql(
        "CREATE TABLE IF NOT EXISTS recurring_task_runs ("
        "id INT AUTO_INCREMENT PRIMARY KEY, "
        "task_id INT NOT NULL, "
        "executed_at DATETIME NOT NULL, "
        "previous_due_date DATE NULL, "
        "next_due_date DATE NULL, "
        "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "INDEX idx_recurring_task_runs_task (task_id), "
        "INDEX idx_recurring_task_runs_executed_at (executed_at), "
        "CONSTRAINT fk_recurring_task_runs_task "
        "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE ON UPDATE CASCADE"
        ")"
    )

    commit()
    _schema_bootstrapped = True
