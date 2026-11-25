"""Avisos automáticos por etiqueta GTD (EnSeguimiento, Agenda): un envío por rol, tarea,
fecha programada y destino. La fila de destino 'app' es la entrada de la campana."""


def upgrade(ctx):
    ctx.exec_sql(
        "CREATE TABLE IF NOT EXISTS role_alert_deliveries ("
        "id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
        "role VARCHAR(32) NOT NULL, "             # rol GTD: following | agenda
        "task_id INT NOT NULL, "
        "scheduled_at_utc DATETIME NOT NULL, "
        "target VARCHAR(40) NOT NULL, "           # telegram | push:<id suscripción> | app
        "status VARCHAR(16) NOT NULL, "           # sent | failed | invalid
        "attempts INT NOT NULL DEFAULT 0, "
        "updated_at_utc DATETIME NOT NULL, "
        "last_error VARCHAR(255) DEFAULT NULL, "
        "read_at_utc DATETIME DEFAULT NULL, "     # solo destino 'app' (leída en la campana)
        "UNIQUE KEY uq_role_alert (role, task_id, scheduled_at_utc, target), "
        "KEY idx_role_alert_app (target, status, updated_at_utc), "
        "CONSTRAINT fk_role_alert_task FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
