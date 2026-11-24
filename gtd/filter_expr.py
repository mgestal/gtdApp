"""Expresiones de filtros guardados: tokenizador, parser y compilación a SQL."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, List, Optional, Tuple

from gtd.utils import _today_madrid


class FilterParseError(Exception):
    pass


def tokenize_filter(expr: str):
    import re

    # Convierte una expresión de filtro de usuario a tokens.
    # Soporta:
    #   - operadores lógicos: &, |, !
    #   - agrupación: ( )
    #   - etiquetas: @NextAction
    #   - comparadores de fecha: fecha<hoy, fecha <= 25-03-2026, due>=+7
    #   - prefijos como p:proyecto, f:carpeta, fr:carpeta-recursiva, fa:carpeta-anywhere,
    #     pf:proyectORfolder, prioridad:alta|media|baja
    #   - identificadores especiales: inbox, done
    tokens = []

    if not expr:
        return tokens

    # normalizar operadores escritos
    expr = re.sub(r"\band\b", "&", expr, flags=re.I)
    expr = re.sub(r"\bor\b", "|", expr, flags=re.I)

    parts = re.split(r"(\||&|\(|\)|!)", expr)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if part == "|":
            tokens.append(("OP","|"))
            continue

        if part == "&":
            tokens.append(("OP","&"))
            continue

        if part == "!":
            tokens.append(("OP","!"))
            continue

        if part == "(":
            tokens.append(("LPAREN","("))
            continue

        if part == ")":
            tokens.append(("RPAREN",")"))
            continue

        if part.startswith("@"):
            tokens.append(("TAG",part[1:]))
            continue

        # Comparadores de fecha: fecha|due + operador + referencia
        # Ejemplos válidos:
        #   fecha < hoy
        #   fecha<=25-03-2026
        #   due>=+7
        m_date = re.fullmatch(r"(?i)(fecha|due)\s*(<=|>=|=|<|>)\s*(.+)", part)
        if m_date:
            op = m_date.group(2)
            ref = m_date.group(3).strip()
            if not ref:
                raise FilterParseError("Falta referencia en comparación de fecha.")
            tokens.append(("DATECMP", f"{op} {ref}"))
            continue

        if ":" in part:
            tokens.append(("TERM",part))
            continue

        tokens.append(("IDENT",part))

    return tokens

# AST nodes
class Node: pass
class Term(Node):
    def __init__(self, kind: str, value: str):
        self.kind = kind  # TAG, IDENT, TERM, DATECMP
        self.value = value

class Not(Node):
    def __init__(self, child: Node): self.child = child

class And(Node):
    def __init__(self, left: Node, right: Node): self.left, self.right = left, right

class Or(Node):
    def __init__(self, left: Node, right: Node): self.left, self.right = left, right


def parse_filter_expression(expr: str) -> Node:
    tokens = tokenize_filter(expr)
    pos = 0

    def cur():
        return tokens[pos] if pos < len(tokens) else None

    def eat(ttype=None, tval=None):
        nonlocal pos
        tok = cur()
        if tok is None:
            return None
        if ttype and tok[0] != ttype:
            return None
        if tval and tok[1] != tval:
            return None
        pos += 1
        return tok

    # Grammar:
    # expr := or_expr
    # or_expr := and_expr ( '|' and_expr )*
    # and_expr := not_expr ( '&' not_expr )*
    # not_expr := '!' not_expr | primary
    # primary := TERM | TAG | IDENT | DATECMP | '(' expr ')'
    #
    # Esto permite expresiones complejas como:
    #   @NextAction & !done
    #   p:Inbox | f:Work
    #   (p:ProjectA & @urgent) | due<=hoy

    def parse_primary() -> Node:
        tok = cur()
        if tok is None:
            raise FilterParseError("Expresión incompleta.")
        if eat("LPAREN"):
            node = parse_or()
            if not eat("RPAREN"):
                raise FilterParseError("Falta ')'.")
            return node
        if tok[0] in ("TERM", "TAG", "IDENT", "DATECMP"):
            eat(tok[0])
            return Term(tok[0], tok[1])
        raise FilterParseError(f"Token inesperado: {tok}")

    def parse_not() -> Node:
        tok = cur()
        if tok and tok[0] == "OP" and tok[1] == "!":
            eat("OP", "!")
            return Not(parse_not())
        return parse_primary()

    def parse_and() -> Node:
        node = parse_not()
        while True:
            tok = cur()
            if tok and tok[0] == "OP" and tok[1] == "&":
                eat("OP", "&")
                node = And(node, parse_not())
            else:
                break
        return node

    def parse_or() -> Node:
        node = parse_and()
        while True:
            tok = cur()
            if tok and tok[0] == "OP" and tok[1] == "|":
                eat("OP", "|")
                node = Or(node, parse_and())
            else:
                break
        return node

    ast = parse_or()
    if pos != len(tokens):
        raise FilterParseError("Tokens sobrantes al final de la expresión.")
    return ast


def _parse_date_ref(ref: str) -> Tuple[Optional[date], bool]:
    """
    Returns (date_value, is_null_ref)
    """
    r = (ref or "").strip()
    if r.lower() == "null":
        return None, True
    if r.lower() == "hoy":
        return _today_madrid(), False
    # integer days from today: N, +N, -N
    if re.fullmatch(r"[+-]?\d+", r):
        return _today_madrid() + timedelta(days=int(r)), False
    # dd-mm-aaaa
    try:
        return datetime.strptime(r, "%d-%m-%Y").date(), False
    except ValueError:
        raise FilterParseError(f"Referencia de fecha inválida: '{ref}'. Usa hoy, NULL, N, +N, -N o dd-mm-aaaa.")



def ast_contains_done(n: Node) -> bool:
    """Detecta si el AST contiene el identificador 'done'."""
    if isinstance(n, Term):
        return n.kind == "IDENT" and n.value.lower() == "done"
    if isinstance(n, Not):
        return ast_contains_done(n.child)
    if isinstance(n, And):
        return ast_contains_done(n.left) or ast_contains_done(n.right)
    if isinstance(n, Or):
        return ast_contains_done(n.left) or ast_contains_done(n.right)
    return False


def compile_filter_to_sql(ast: Node) -> Tuple[str, List[Any]]:
    """
    Compila AST a WHERE SQL + parámetros.

    Prefijos soportados:
      @tag      => tareas con etiqueta
      p:name    => proyecto exacto
      f:name    => carpeta directa exacta
      fr:name   => folder recursivo (tareas de folder_id en el árbol)
      fa:name   => folder anywhere (folder + proyectos en el árbol)
      pf:value  => búsqueda libre en proyecto/carpeta/field
            prioridad:value / pr:value => prioridad (alta|media|baja|1|2|3|null)

    Tokens de comparadores de fechas (DATECMP) se interpretan en _parse_date_ref().
    """

    params: List[Any] = []

    def compile_node(n: Node) -> str:

        if isinstance(n, And):
            return f"({compile_node(n.left)} AND {compile_node(n.right)})"

        if isinstance(n, Or):
            return f"({compile_node(n.left)} OR {compile_node(n.right)})"

        if isinstance(n, Not):
            return f"(NOT {compile_node(n.child)})"

        if isinstance(n, Term):

            if n.kind == "IDENT":

                v = n.value.lower()

                if v == "inbox":
                    return "(t.project_id IS NULL AND t.folder_id IS NULL)"

                if v == "done":
                    return "(t.completed_at IS NOT NULL)"

                if v == "null":
                    raise FilterParseError("NULL debe usarse con p:, f: o fecha = NULL.")

                raise FilterParseError(f"Identificador desconocido: {n.value}")

            if n.kind == "TAG":

                params.append(n.value)

                return (
                    "EXISTS (SELECT 1 FROM task_tags tt "
                    "JOIN tags tg ON tg.id=tt.tag_id "
                    "WHERE tt.task_id=t.id AND tg.name=%s)"
                )

            if n.kind == "TERM":

                term = n.value
                prefix, val = term.split(":", 1)

                prefix = prefix.lower()
                val_str = (val or "").strip()

                # Permite valores entre comillas en filtros tipo p:, f:, fr:, fa:, pf:
                # Ejemplo: fa:"SomeTime"
                if len(val_str) >= 2 and (
                    (val_str[0] == '"' and val_str[-1] == '"')
                    or (val_str[0] == "'" and val_str[-1] == "'")
                ):
                    val_str = val_str[1:-1].strip()

                # -----------------------------
                # NULL handling
                # -----------------------------

                if val_str.lower() == "null":

                    if prefix == "p":
                        return "(t.project_id IS NULL)"

                    if prefix == "f":
                        return "(t.folder_id IS NULL)"

                    raise FilterParseError("NULL debe usarse con p: o f:")

                # -----------------------------
                # p: proyecto
                # -----------------------------

                if prefix == "p":

                    params.append(val_str)
                    return "(p.name=%s)"

                # -----------------------------
                # f: carpeta directa
                # -----------------------------

                if prefix == "f":

                    params.append(val_str)
                    return "(fd.name=%s)"

                # -----------------------------
                # fr: carpeta recursiva
                # Solo tareas con folder_id dentro del árbol
                # -----------------------------

                if prefix == "fr":

                    params.append(val_str)

                    return (
                        "((t.folder_id IS NOT NULL) AND t.folder_id IN ("
                        "WITH RECURSIVE subfolders AS ("
                        " SELECT id FROM folders WHERE name=%s"
                        " UNION ALL"
                        " SELECT f.id FROM folders f"
                        " JOIN subfolders sf ON f.parent_id = sf.id"
                        ") "
                        "SELECT id FROM subfolders"
                        "))"
                    )

                # -----------------------------
                # fa: folder anywhere
                # Incluye:
                #  - tareas con folder_id en la carpeta o cualquier subcarpeta
                #  - tareas de proyectos cuyo p.folder_id esté en la carpeta
                #    o cualquier subcarpeta
                # -----------------------------

                if prefix == "fa":

                    params.append(val_str)
                    params.append(val_str)

                    return (
                        "("
                        "((t.folder_id IS NOT NULL) AND t.folder_id IN ("
                        "WITH RECURSIVE subfolders AS ("
                        " SELECT id FROM folders WHERE name=%s"
                        " UNION ALL"
                        " SELECT f.id FROM folders f"
                        " JOIN subfolders sf ON f.parent_id = sf.id"
                        ") "
                        "SELECT id FROM subfolders"
                        ")) "
                        "OR "
                        "((p.folder_id IS NOT NULL) AND p.folder_id IN ("
                        "WITH RECURSIVE subfolders AS ("
                        " SELECT id FROM folders WHERE name=%s"
                        " UNION ALL"
                        " SELECT f.id FROM folders f"
                        " JOIN subfolders sf ON f.parent_id = sf.id"
                        ") "
                        "SELECT id FROM subfolders"
                        "))"
                        ")"
                    )

                # -----------------------------
                # pf: búsqueda proyecto/carpeta
                # -----------------------------

                if prefix == "pf":

                    if "/" in val_str:

                        folder_name, project_name = val_str.split("/", 1)

                        folder_name = folder_name.strip()
                        project_name = project_name.strip()

                        if not folder_name or not project_name:
                            raise FilterParseError('pf:"Carpeta/Proyecto" requiere ambos nombres.')

                        params.append(project_name)
                        params.append(folder_name)

                        return "(p.name=%s AND pf.name=%s)"

                    kw = f"%{val_str.lower()}%"

                    params.extend([kw, kw, kw])

                    return (
                        "("
                        "LOWER(p.name) LIKE %s "
                        "OR LOWER(fd.name) LIKE %s "
                        "OR LOWER(pf.name) LIKE %s"
                        ")"
                    )

                # -----------------------------
                # prioridad: / pr:
                # -----------------------------

                if prefix in ("prioridad", "priority", "prio", "pr"):
                    v = val_str.lower()

                    if v in ("1", "alta", "high"):
                        params.append(1)
                        return "(t.priority=%s)"

                    if v in ("2", "media", "medium"):
                        params.append(2)
                        return "(t.priority=%s)"

                    if v in ("3", "baja", "low"):
                        params.append(3)
                        return "(t.priority=%s)"

                    if v in ("null", "none", "sin", "ninguna"):
                        return "(t.priority IS NULL)"

                    raise FilterParseError(
                        "Prioridad inválida. Usa prioridad:alta|media|baja|1|2|3|null."
                    )

                raise FilterParseError(f"Prefijo desconocido: {prefix}:")

            if n.kind == "DATECMP":

                op, ref = n.value.split(" ", 1)

                d, is_null = _parse_date_ref(ref)

                if is_null:

                    if op != "=":
                        return "(1=0)"

                    return "(t.due_date IS NULL)"

                params.append(d)

                return f"(t.due_date IS NOT NULL AND t.due_date {op} %s)"

            raise FilterParseError("Término no soportado.")

        raise FilterParseError("AST inválido.")

    where = compile_node(ast)

    return where, params
