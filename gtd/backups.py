"""Copias de seguridad: listado, mysqldump y restauración."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Tuple

from gtd.config import BACKUP_DIR, load_config


def list_backups() -> List[str]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in BACKUP_DIR.glob("*.sql"):
        if p.is_file():
            files.append(p.name)
    return sorted(files, reverse=True)


def list_archive_task_backups() -> List[str]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in BACKUP_DIR.glob("archive_tasks_*.json"):
        if p.is_file():
            files.append(p.name)
    return sorted(files, reverse=True)

def run_mysqldump_to_file(filepath: Path) -> Tuple[bool, str]:
    cfg = load_config()
    dbcfg = cfg["db"]
    cmd = [
        "/usr/bin/mysqldump",
        f"--host={dbcfg['host']}",
        f"--port={dbcfg['port']}",
        f"--user={dbcfg['user']}",
        f"--password={dbcfg['password']}",
        "--default-character-set=utf8mb4",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--events",
        "--add-drop-table",
        dbcfg["database"],
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if res.returncode != 0:
            return False, res.stderr.strip() or "mysqldump falló"
        filepath.write_text(res.stdout, encoding="utf-8")
        return True, "OK"
    except Exception as e:
        return False, str(e)

def run_mysql_import_from_file(filepath: Path) -> Tuple[bool, str]:
    cfg = load_config()
    dbcfg = cfg["db"]
    cmd = [
        "/usr/bin/mysql",
        f"--host={dbcfg['host']}",
        f"--port={dbcfg['port']}",
        f"--user={dbcfg['user']}",
        f"--password={dbcfg['password']}",
        "--default-character-set=utf8mb4",
        dbcfg["database"],
    ]
    try:
        sql = filepath.read_text(encoding="utf-8")
        res = subprocess.run(cmd, input=sql, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if res.returncode != 0:
            return False, res.stderr.strip() or "mysql import falló"
        return True, "OK"
    except Exception as e:
        return False, str(e)
