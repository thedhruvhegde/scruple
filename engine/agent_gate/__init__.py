"""Deprecated import path. ``agent_gate`` is now ``scruple``.

``import agent_gate.x`` returns the very same module object as ``import scruple.x``.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
import types
import warnings

warnings.warn("agent_gate is now scruple; update your imports", DeprecationWarning, stacklevel=2)


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname.startswith("agent_gate."):
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        return importlib.import_module("scruple" + spec.name[len("agent_gate") :])

    def exec_module(self, module: types.ModuleType) -> None:
        return None


sys.meta_path.insert(0, _Alias())
sys.modules[__name__] = importlib.import_module("scruple")
