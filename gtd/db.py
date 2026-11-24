"""Conexión MySQL por petición y helpers q/q1/exec_sql/commit/rollback."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pymysql
from flask import g

from gtd.config import load_config
from gtd.core import app


def get_db_conn():
    cfg = load_config()
    db = cfg.get("db", {})
    return pymysql.connect(
        host=db.get("host"),
        port=int(db.get("port", 3306)),
        user=db.get("user"),
        password=db.get("password"),
        database=db.get("database"),
        charset=db.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


@app.teardown_request
def _teardown(exc):
    conn = getattr(g, "db_conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass

def db():
    if "db_conn" not in g:
        g.db_conn = get_db_conn()
    try:
        g.db_conn.ping(reconnect=True)
    except Exception:
        g.db_conn = get_db_conn()
        g.db_conn.ping(reconnect=True)
    return g.db_conn

def q(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    with db().cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())

def q1(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    rows = q(sql, params)
    return rows[0] if rows else None


def exec_sql(sql: str, params: Tuple[Any, ...] = ()) -> int:
    with db().cursor() as cur:
        cur.execute(sql, params)
        return cur.lastrowid or 0

def commit():
    db().commit()

def rollback():
    db().rollback()
