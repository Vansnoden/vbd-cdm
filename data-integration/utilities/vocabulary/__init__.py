# -*- coding: utf-8 -*-
"""
The vocabulary layer: ingestion, registry, normalisers, resolution.
"""

from __future__ import annotations

import importlib
from typing import Any, List

_LAZY = frozenset({"build", "diseases", "loader", "locations", "species"})

__all__: List[str] = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    """Resolve one of :data:`_LAZY` on first access (PEP 562).

    Parameters
    ----------
    name : str
        The attribute being looked up.

    Returns
    -------
    module
        The imported submodule, also cached in this module's globals so the
        next lookup never reaches this function.

    Raises
    ------
    AttributeError
        For any name that is not a lazy submodule. This is required, not
        defensive: ``from utilities.vocabulary import registry`` depends on
        ``AttributeError`` here so that the import system falls back to
        importing ``utilities.vocabulary.registry``.
    """
    if name in _LAZY:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module

    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}. "
        f"Lazily provided submodules: {', '.join(sorted(_LAZY))}. "
        f"The registry, normalisers, field catalog and strategies are ordinary "
        f"submodules — import them as utilities.vocabulary.<name>."
    )


def __dir__() -> List[str]:
    """Include the not-yet-imported lazy names in ``dir()``."""
    return sorted(set(globals()) | set(_LAZY))