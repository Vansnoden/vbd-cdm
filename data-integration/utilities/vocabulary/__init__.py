# -*- coding: utf-8 -*-
"""
The vocabulary layer: ingestion, registry, normalisers, resolution.

Why this file is lazy (finding §3.2)
------------------------------------
It used to import ``build``, ``diseases``, ``loader``, ``locations`` and
``species`` eagerly. Those five modules are the *build tooling* for the shipped
vocabulary CSVs, and between them they import ``pronto`` (IDOMAL/OBO parsing),
``Bio`` / ``Bio.Entrez`` (NCBI fetch) and ``geopandas`` (GADM boundary
extraction). So ``from utilities.vocabulary import normalizers`` — which
``registry.py``, ``consolidation.py`` and ``field_catalog.py`` all do at module
scope — dragged the whole build toolchain into every process before any
pipeline work started. A checkout without ``pronto`` installed could not import
the resolver at all, and the failure pointed at a module the caller had never
named.

Batch 1 flagged this and deferred it to "the batch that rewrites the loader";
batch 2 rewrote the loader's *functions* and did not touch this file. It is
fixed here with PEP 562 module-level ``__getattr__``: the five heavy names are
resolved on first attribute access and then cached in the module globals, so
``utilities.vocabulary.build`` works exactly as it did, while importing the
package costs nothing.

What is *not* in ``_LAZY``, and why
-----------------------------------
``normalizers``, ``registry``, ``field_catalog`` and ``strategies`` are the new,
light modules. They are ordinary submodules: ``from utilities.vocabulary import
registry`` falls through to the import machinery and works whether or not they
are named here. Adding them to ``_LAZY`` would only mean this file had to be
edited every time one was added, so they are deliberately absent.

Two behaviours worth stating
----------------------------
* ``__getattr__`` raises ``AttributeError`` — never ``ImportError`` — for an
  unknown name. ``from utilities.vocabulary import normalizers`` relies on that:
  CPython tries the attribute, catches ``AttributeError``, and falls back to
  importing the submodule. Raising anything else would break the fallback.
* The lazy import is cached in ``globals()``, so a second access is a plain
  dictionary hit and ``hasattr(utilities.vocabulary, "build")`` is idempotent.

Module-level assumptions
------------------------
* Every name in ``_LAZY`` is a real submodule of this package.
* ``importlib`` is stdlib; this module imports nothing else.
"""

from __future__ import annotations

import importlib
from typing import Any, List

#: Submodules resolved on first attribute access. These are the build toolchain:
#: between them they pull ``pronto``, ``Bio`` and ``geopandas``.
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