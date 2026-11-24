"""Línea base: esquema que antes aplicaba ensure_schema_updates() en cada arranque.

Supone que las tablas principales existen (sql/esquema.sql). Es idempotente
(IF NOT EXISTS / comprobaciones previas), así que es seguro en BD ya actualizadas.
"""
from browser_push import PUSH_SCHEMA
from reminders import REMINDER_SCHEMA


def upgrade(ctx):
    for statement in REMINDER_SCHEMA:
        ctx.exec_sql(statement)
    ctx.exec_sql("ALTER TABLE task_reminders ADD COLUMN IF NOT EXISTS send_telegram TINYINT(1) NOT NULL DEFAULT 1")
    ctx.exec_sql("ALTER TABLE task_reminders ADD COLUMN IF NOT EXISTS send_browser TINYINT(1) NOT NULL DEFAULT 0")
    for statement in PUSH_SCHEMA:
        ctx.exec_sql(statement)
    ctx.exec_sql(
        "CREATE TABLE IF NOT EXISTS tag_locations ("
        "tag_id INT NOT NULL PRIMARY KEY, latitude DOUBLE NOT NULL, "
        "longitude DOUBLE NOT NULL, radius_m DOUBLE NOT NULL, "
        "CONSTRAINT fk_tag_location_tag FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    ctx.exec_sql(
        "CREATE TABLE IF NOT EXISTS notification_reads ("
        "reminder_id INT NOT NULL, scheduled_at_utc DATETIME NOT NULL, read_at_utc DATETIME NOT NULL, "
        "PRIMARY KEY (reminder_id, scheduled_at_utc), "
        "CONSTRAINT fk_notification_read_reminder FOREIGN KEY (reminder_id) "
        "REFERENCES task_reminders(id) ON DELETE CASCADE"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )

    # Normalize tag types while preserving values from installations that still
    # have the original free-text `tags.type` column.
    ctx.exec_sql(
        "CREATE TABLE IF NOT EXISTS TagsType ("
        "id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
        "name VARCHAR(80) NOT NULL, UNIQUE KEY uq_tags_type_name (name)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    ctx.exec_sql("ALTER TABLE tags ADD COLUMN IF NOT EXISTS type_id INT NULL AFTER name")
    tag_columns = ctx.q("SHOW COLUMNS FROM tags") or []
    tag_column_names = {row.get("Field") for row in tag_columns}
    if "type" in tag_column_names:
        ctx.exec_sql(
            "INSERT IGNORE INTO TagsType(name) "
            "SELECT DISTINCT TRIM(type) FROM tags "
            "WHERE type IS NOT NULL AND TRIM(type) <> ''"
        )
        ctx.exec_sql(
            "UPDATE tags tg JOIN TagsType tt ON tt.name=TRIM(tg.type) "
            "SET tg.type_id=tt.id WHERE tg.type_id IS NULL"
        )
        ctx.exec_sql("ALTER TABLE tags DROP COLUMN type")
    type_fk = ctx.q1(
        "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tags' "
        "AND COLUMN_NAME='type_id' AND REFERENCED_TABLE_NAME='TagsType' LIMIT 1"
    )
    if not type_fk:
        ctx.exec_sql(
            "ALTER TABLE tags ADD CONSTRAINT fk_tags_type "
            "FOREIGN KEY (type_id) REFERENCES TagsType(id) "
            "ON DELETE SET NULL ON UPDATE CASCADE"
        )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS due_time TIME NULL AFTER due_date"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS location VARCHAR(1024) NULL AFTER notes"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS archived TINYINT(1) NOT NULL DEFAULT 0 AFTER recurrence_rule"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS archived_at DATETIME NULL AFTER archived"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS deleted_at DATETIME NULL AFTER archived_at"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS deleted_prev_archived TINYINT(1) NOT NULL DEFAULT 0 AFTER deleted_at"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS priority TINYINT NULL DEFAULT NULL AFTER recurrence_rule"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "MODIFY COLUMN priority TINYINT NULL DEFAULT NULL"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS kanban_status VARCHAR(20) NOT NULL DEFAULT 'inbox' AFTER sort_order"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS project_board_status VARCHAR(80) NULL AFTER kanban_status"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS project_board_order INT NULL AFTER project_board_status"
    )
    ctx.exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS archived_at DATETIME NULL AFTER archived"
    )
    ctx.exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS deleted_at DATETIME NULL AFTER archived_at"
    )
    ctx.exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS deleted_prev_archived TINYINT(1) NOT NULL DEFAULT 0 AFTER deleted_at"
    )
    ctx.exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS board_columns JSON NULL AFTER board_visible"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_archived (archived)"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_archived_at (archived_at)"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_deleted_at (deleted_at)"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_priority (priority)"
    )
    ctx.exec_sql(
        "ALTER TABLE projects "
        "ADD INDEX IF NOT EXISTS idx_projects_archived_at (archived_at)"
    )
    ctx.exec_sql(
        "ALTER TABLE projects "
        "ADD INDEX IF NOT EXISTS idx_projects_deleted_at (deleted_at)"
    )

    # Calendar sync metadata (bidireccional)
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS google_calendar_id VARCHAR(255) NULL AFTER archived_at"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS google_event_id VARCHAR(255) NULL AFTER google_calendar_id"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS google_event_etag VARCHAR(255) NULL AFTER google_event_id"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_sync_state VARCHAR(40) NOT NULL DEFAULT 'none' AFTER google_event_etag"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_sync_ignored TINYINT(1) NOT NULL DEFAULT 0 AFTER calendar_sync_state"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_sync_error TEXT NULL AFTER calendar_sync_state"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_last_synced_hash CHAR(64) NULL AFTER calendar_sync_error"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_last_synced_at DATETIME NULL AFTER calendar_last_synced_hash"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_local_changed_at DATETIME NULL AFTER calendar_last_synced_at"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_remote_updated_at DATETIME NULL AFTER calendar_local_changed_at"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_conflict_payload LONGTEXT NULL AFTER calendar_remote_updated_at"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_conflict_at DATETIME NULL AFTER calendar_conflict_payload"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_google_event_id (google_event_id)"
    )
    ctx.exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_calendar_sync_state (calendar_sync_state)"
    )

    ctx.exec_sql(
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
