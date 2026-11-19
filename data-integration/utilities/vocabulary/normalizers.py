# -*- coding: utf-8 -*-
"""
Named, pure normalisation functions, addressed by id from a pack manifest.

A manifest names a chain by id (``normalizer_chain: [species_canon]``) and the
resolver composes it with :func:`compose`. Nothing in this module reads a
manifest, a database or the filesystem: every function here is a pure
``str -> str``, which is what makes a resolution reproducible from
``(value, chain)`` alone.

This module is the ONLY definition of ``_norm_species`` and ``_norm_deep`` in
the codebase. ``utilities/vocabulary/loader.py`` previously carried a second,
module-level copy (review §6.3); batch 6A's full-file replacement of that
module deletes it, leaving this file as the single source. Do not reintroduce a
copy anywhere: two copies of a regex mean two answers to "is this the same
species", and the one in the matcher is the one that wins by accident.

The two bodies below are copied BYTE-IDENTICAL from the module-level
definitions that lived in ``utilities/vocabulary/loader.py`` before the
refactor. **Copied 2026-11-18** from that file, and verified by diff at copy
time. Any change to a regex here changes what the existing goldens mean, so it
must be its own reviewed change with a regenerated baseline — not a side effect
of a refactor.

Why ``import pandas`` is load-bearing
-------------------------------------
``_norm_species`` opens with a ``pd.isna(s)`` guard inside
``except (TypeError, ValueError)``. The pre-refactor ``loader.py`` had pandas in
scope; the batch-1 copy of this module did not, so the guard raised
``NameError`` on *every* non-``None`` input — a ``NameError`` that the except
clause did not catch (review §3.1, batch 2 note 1). Three call sites grew
defensive guards for it. Those guards stay (a normaliser must never kill a
run), but a guard that fires on every call is a loud way of being broken, not a
fix. The import below is the fix, and the bodies stay byte-identical.

Module-level assumptions
------------------------
* ``re``, ``unicodedata``, ``pandas`` are importable. ``pandas`` is a hard
  dependency of the pipeline, not an optional extra.
* The three compiled regexes are private to this module and are used by
  ``_norm_species`` only.
* ``NORMALIZERS`` is populated at import time by :func:`register` and is
  treated as read-only afterwards. Insertion order is irrelevant: chains are
  ordered by the manifest, not by registration.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Callable, Iterable, Mapping, Sequence

import pandas as pd  # noqa: F401  required by _norm_species; see module note

logger = logging.getLogger(__name__)


#: A normaliser is a pure ``str -> str`` transform.
Normalizer = Callable[[str], str]

#: id -> function. Populated by :func:`register`; read-only after import.
NORMALIZERS: dict[str, Normalizer] = {}


class UnknownNormalizer(KeyError):
    """Raised when a manifest names a normaliser id that is not registered."""

    def __init__(self, name: str, available: Sequence[str]) -> None:
        self.name = str(name)
        self.available = tuple(available)
        super().__init__(self.name, self.available)

    def __str__(self) -> str:  # KeyError would otherwise repr the tuple
        return (
            f"unknown normalizer {self.name!r}; "
            f"registered ids: {', '.join(self.available) or '(none)'}"
        )


def register(name: str) -> Callable[[Normalizer], Normalizer]:
    """Return a decorator that registers ``fn`` under ``name``.

    Duplicate registration is an error rather than a silent overwrite: two
    modules claiming one id would make matching depend on import order, which
    is exactly the nondeterminism the contract forbids.
    """
    identifier = str(name or "").strip()
    if not identifier:
        raise ValueError("normalizer id must be a non-empty string")

    def _decorator(fn: Normalizer) -> Normalizer:
        existing = NORMALIZERS.get(identifier)
        if existing is not None:
            raise ValueError(
                f"normalizer {identifier!r} is already registered "
                f"({getattr(existing, '__qualname__', existing)!r})"
            )
        NORMALIZERS[identifier] = fn
        return fn

    return _decorator


def available() -> tuple[str, ...]:
    """Registered normaliser ids, sorted. Deterministic, for logging/errors."""
    return tuple(sorted(NORMALIZERS))


def get(name: str) -> Normalizer:
    """Look up a normaliser by id, raising :class:`UnknownNormalizer`."""
    try:
        return NORMALIZERS[str(name)]
    except KeyError:
        raise UnknownNormalizer(str(name), available()) from None


def compose(chain: Sequence[str] | Iterable[str] | None) -> Normalizer:
    """Compose a manifest-declared chain into one ``str -> str``.

    ``compose(["generic"])`` returns the generic normaliser; ``compose([])``
    and ``compose(None)`` return the identity function. Chains are applied left
    to right, so ``["generic", "species_canon"]`` means "deep-normalise, then
    canonicalise the species".

    The returned callable closes over resolved *functions*, not over names, so
    a later re-registration cannot change an already composed chain.
    """
    resolved: list[Normalizer] = [get(name) for name in (chain or ())]

    if not resolved:
        return _identity
    if len(resolved) == 1:
        return resolved[0]

    names = ",".join(str(name) for name in chain)

    def _composed(value: str) -> str:
        out = value
        for fn in resolved:
            out = fn(out)
        return out

    _composed.__name__ = f"compose({names})"
    _composed.__qualname__ = _composed.__name__
    return _composed


def _identity(value: str) -> str:
    """The empty chain: return the value unchanged."""
    return value


# =============================================================================
# Verbatim copies from utilities/vocabulary/loader.py (copied 2026-08-18-era
# module-level definitions; byte-identical, re-verified 2026-11-18).
#
# Do not edit these bodies. Any change to a regex here changes what the
# existing goldens mean; do it as its own reviewed change with a regenerated
# baseline, not as a side effect of a refactor.
# =============================================================================

_SPECIES_QUALIFIER_RE = re.compile(r"\s+(complex|subgroup|group)\s*$")
_SPECIES_STRAIN_RE    = re.compile(r"\s+(?:s\s*form|m\s*form|form)\b.*$")
_SPECIES_GENUS_RE     = re.compile(r"^anopheles\s+")


def _norm_species(s) -> str:
    """
    Species-specific normalisation.

    Collapses case, underscores/hyphens, the genus prefix, and the common
    qualifiers ('complex', 'subgroup', 'group', 's form', 'm form',
    's.l.', 's.s.') so that, for example:

        'Gambiae Complex'
        'gambiae complex'
        'Anopheles gambiae'
        'gambiae_s form'
        'gambiae_s form_m form'

    all reduce to the single key 'gambiae'.

    Deliberately does NOT collapse hybrid designations such as
    'Anopheles coluzzii/gambiae' — those are legitimately distinct and
    should not be merged with either parent species.
    """
    if s is None:
        return ""
    try:
        if pd.isna(s):
            return ""
    except (TypeError, ValueError):
        pass

    s = str(s).strip().lower()
    s = re.sub(r"[_\-]", " ", s)          # underscores/hyphens -> space
    s = re.sub(r"\s+", " ", s)            # collapse whitespace
    s = _SPECIES_GENUS_RE.sub("", s)      # drop leading 'anopheles '
    s = _SPECIES_QUALIFIER_RE.sub("", s)  # drop trailing complex/subgroup/group
    s = re.sub(r"\s+s\.?\s*l\.?$", "", s) # drop trailing 's.l.'
    s = re.sub(r"\s+s\.?\s*s\.?$", "", s) # drop trailing 's.s.'
    s = _SPECIES_STRAIN_RE.sub("", s)     # drop 's form' / 'm form' / 'form ...'
    return s.strip()


def _norm_deep(s) -> str:
    """
    Generic normalisation used for non-species domains:
    strip diacritics, collapse whitespace, lower-case.
    """
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


# =============================================================================
# Registration
#
# Explicit calls rather than decorators, so the two function bodies above stay
# byte-identical to the originals and can still be diffed against the
# pre-refactor file.
# =============================================================================

register("species_canon")(_norm_species)
register("generic")(_norm_deep)


__all__ = [
    "NORMALIZERS",
    "Normalizer",
    "UnknownNormalizer",
    "available",
    "compose",
    "get",
    "register",
]