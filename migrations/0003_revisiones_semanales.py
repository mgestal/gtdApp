"""Revisiones semanales: una fila por revisión (en curso si finished_at es NULL)."""


def upgrade(ctx):
    ctx.exec_sql(
        "CREATE TABLE IF NOT EXISTS weekly_reviews ("
        "id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
        "started_at DATETIME NOT NULL, "
        "finished_at DATETIME NULL, "
        "checked_blocks VARCHAR(255) NOT NULL DEFAULT '', "
        "KEY idx_weekly_reviews_finished (finished_at)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
