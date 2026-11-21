# -*- coding: utf-8 -*-
"""
Named, pure normalisation functions, addressed by id from a pack manifest.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Callable, Iterable, Mapping, Sequence
from typing import Any, Dict, List, Optional

import pandas as pd  # noqa: F401  required by _norm_species; see module note

logger = logging.getLogger(__name__)


Normalizer = Callable[[str], str]

NORMALIZERS: dict[str, Normalizer] = {}


class UnknownNormalizer(KeyError):
    """Raised when a manifest names a normaliser id that is not registered."""

    def __init__(self, name: str, available: Sequence[str]) -> None:
        self.name = str(name)
        self.available = tuple(available)
        super().__init__(self.name, self.available)

    def __str__(self) -> str:  
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


_SPECIES_QUALIFIER_RE = re.compile(r"\s+(complex|subgroup|group)\s*$")
_SPECIES_STRAIN_RE    = re.compile(r"\s+(?:s\s*form|m\s*form|form)\b.*$")
_SPECIES_GENUS_RE     = re.compile(r"^anopheles\s+")


def _norm_species(s) -> str:
    """
    Species-specific normalisation.
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