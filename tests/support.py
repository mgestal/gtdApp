"""Utilidades comunes para los tests.

La app está dividida en módulos (paquete ``gtd``) que importan helpers por nombre
(``from gtd.db import q``). Parchear ``gtd.db.q`` no afecta a esas copias, así que
``patch_all`` sustituye el nombre en *todos* los módulos donde está enlazado.
"""
from __future__ import annotations

import contextlib
import sys
from unittest.mock import MagicMock, patch

import app  # noqa: F401  carga y registra todos los módulos de gtd


def _modules():
    return [m for name, m in list(sys.modules.items())
            if m is not None and (name == "app" or name == "gtd" or name.startswith("gtd."))]


class patch_all:
    """Como ``patch.object`` pero en todos los módulos de ``gtd`` que tengan ``name``.

    Solo se parchean los módulos cuyo ``name`` es el mismo objeto que la definición
    original (no se tocan homónimos distintos). Sirve como context manager o con
    ``start()``/``stop()``; ambos devuelven el mock (o ``new``).
    """

    def __init__(self, name, new=None, **kwargs):
        self.name = name
        self.new = new
        self.kwargs = kwargs
        self._stack = None

    def start(self):
        owners = [m for m in _modules() if self.name in vars(m)]
        if not owners:
            raise AttributeError(f"Ningún módulo de gtd define {self.name!r}")
        # Valor original: el del módulo que lo define (p.ej. la función ``search`` y no el
        # submódulo ``gtd.views.search``, que el paquete ``gtd.views`` también expone).
        defining = [m for m in owners if getattr(vars(m)[self.name], "__module__", None) == m.__name__]
        original = vars((defining or owners)[0])[self.name]
        replacement = self.new if self.new is not None else MagicMock(**self.kwargs)
        self._stack = contextlib.ExitStack()
        for module in owners:
            if vars(module)[self.name] is original:
                self._stack.enter_context(patch.object(module, self.name, replacement))
        return replacement

    def stop(self):
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
