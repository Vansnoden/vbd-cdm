# -*- coding: utf-8 -*-
"""
The deterministic concept resolver: one raw value in, one recorded decision out.

This module implements the six-rung ladder of review §3 step 5 and nothing else.
It does not read manifests (a ``ConceptPack`` is handed to it), it does not write
to the database (it returns a ``Resolution``; the ``fact_concept_map`` upsert and
the ``fact_resolution_log`` append belong to the caller), and it does not choose
between packs unless it is asked to by passing ``pack=None``.

The ladder
----------
======================  ==========  ==================================================
rung                    score       accepted when
======================  ==========  ==================================================
``exact``               1.00        ``concept_name`` equals the probe
``normalized``          0.98        ``normalise(concept_name)`` equals ``normalise(probe)``
``alias``               0.95        a synonym equals the probe, exactly or normally
``fuzzy``               ratio/100   ``ratio / 100 >= pack.fuzzy_threshold``
                                    **and** ``_apply_margin(best, runner_up, pack)``
``ambiguous``           best        that same fuzzy rung with the margin failing:
                                    ``decision = needs_review``, ``concept_code = None``
``unresolved``          0.00        no rung matched (including an empty probe)
======================  ==========  ==================================================

The rungs are tried in that order and the first that matches decides. Nothing is
added, removed or reordered. ``taxonomy_rollup`` is deferred per review §6 and is
therefore absent by design, not by oversight.

Threshold and margin
--------------------
``pack.fuzzy_threshold`` and ``pack.fuzzy_margin`` are the only knobs, and both
are read from the pack on every call. There is no default for either anywhere in
this module: a caller with no pack has no threshold, and inventing 0.85 here
would be the policy-as-literal the review keeps objecting to. Scores are compared
in the [0, 1] domain, never against ``threshold * 100``, because ``0.7 * 100`` is
``70.00000000000001`` and would reject an honest score of 70.

Why the pack is duck-typed
--------------------------
Everything read from a pack is either a contract field (``pack_id``, ``version``,
``domain``, ``normalizer_chain``, ``fuzzy_threshold``, ``fuzzy_margin``,
``case_sensitive``, ``field_scope``) or ``pack.normalizer()``. Nothing here
imports ``utilities.vocabulary.registry``, so this module is importable in an
environment where that module's optional dependencies are absent, and a test can
drive it with a twenty-line stub pack that counts its own calls.

Determinism
-----------
* every query against ``dim_concept`` is ``ORDER BY concept_code``;
* fuzzy candidates are ranked ``(-score, -concept_rank, concept_code)``, never by
  row order, dict order or set order;
* ``candidates`` JSON is serialised with ``sort_keys=True`` and fixed separators,
  so two runs produce byte-identical text;
* the in-run indexes are built from the ordered query, so a duplicate name
  resolves by ``concept_rank`` and then by ``concept_code``.

Cache
-----
``resolve`` memoises on ``(field, value)``. A chunked source hands the same raw
value to the resolver once per chunk; without memoisation each chunk would
produce its own ledger row and the append-only table would carry duplicates of a
single decision. One resolver instance is therefore scoped to one run: it reads
the vocabulary into memory once and must not outlive the vocabulary load that
preceded it.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

import rapidfuzz
from rapidfuzz import fuzz, process
from sqlalchemy import text

# The four parsing helpers live in `utilities.vocabulary.loader` and are imported
# rather than re-implemented. The previous version of this module imported them
# *and* defined its own copies below, so `_as_text` shadowed the import three
# lines after a docstring asserting that it *was* the loader's function
# (review §6.2/§6.4). One definition, one behaviour.
from utilities.vocabulary.loader import (
    _clean_text as _as_text,
    _concept_rank as _rank_of,
    _parse_extra,
    _split_synonyms,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Decision enum (interface contract)
#
# `rejected` is a human verdict and is never produced by this module.
# =============================================================================

AUTO_ACCEPTED = "auto_accepted"
NEEDS_REVIEW = "needs_review"
REJECTED = "rejected"
UNRESOLVED = "unresolved"

DECISIONS: frozenset = frozenset(
    {AUTO_ACCEPTED, NEEDS_REVIEW, REJECTED, UNRESOLVED}
)

#: Ladder method names (interface contract).
METHOD_EXACT = "exact"
METHOD_NORMALIZED = "normalized"
METHOD_ALIAS = "alias"
METHOD_FUZZY = "fuzzy"
METHOD_AMBIGUOUS = "ambiguous"
METHOD_UNRESOLVED = "unresolved"

#: The three labels written to ``fact_insecticide_resistance.resistance_status``.
#: These are CDM values, not thresholds: they name the bands the cut-offs
#: delimit, and every downstream query groups by them, so they stay code and are
#: spelled in exactly one place.
RESISTANCE_BANDS = MappingProxyType(
    {
        "susceptible": "Susceptible",
        "suspected": "Suspected Resistance",
        "resistant": "Resistant",
    }
)


# =============================================================================
# Provenance
# =============================================================================

#: Recorded on every Resolution so a decision can be attributed to a tool
#: release. Bumped when the ladder's behaviour changes.
RESOLVER_VERSION = "1.1.0"

#: Pinned library recorded in the run manifest. ``token_set_ratio`` scores can
#: move between rapidfuzz releases, so the version is part of the provenance
#: rather than an undeclared dependency.
FUZZY_LIBRARY_VERSION = f"rapidfuzz=={rapidfuzz.__version__}"


# =============================================================================
# Ladder constants
#
# These are rung scores, not policy knobs. The knobs (fuzzy_threshold,
# fuzzy_margin) are read from the pack and never defaulted.
# =============================================================================

EXACT_SCORE = 1.00
NORMALIZED_SCORE = 0.98
ALIAS_SCORE = 0.95
UNRESOLVED_SCORE = 0.00

#: How many candidates a Resolution carries as evidence.
DEFAULT_CANDIDATE_LIMIT = 5

#: Scores are rounded only in the JSON evidence, so the text is stable and
#: readable while ``Resolution.match_score`` keeps the exact ratio.
SCORE_PRECISION = 6

#: RapidFuzz compares ``score_cutoff`` in percent. Subtracting this tolerance only
#: ever admits a candidate sitting exactly on the boundary, which the margin rule
#: may still need; it can never exclude a decisive one.
_CUTOFF_TOLERANCE = 1e-9


_CONCEPT_QUERY = """
    SELECT concept_code, concept_name, extra
    FROM dim_concept
    WHERE {where}
    ORDER BY concept_code
"""

_DOMAIN_BREAKDOWN_QUERY = """
    SELECT domain, COUNT(*)
    FROM dim_concept
    WHERE vocabulary_id = :v
    GROUP BY domain
    ORDER BY domain
"""

_LEGACY_ID_QUERY = """
    SELECT COUNT(*)
    FROM dim_concept
    WHERE vocabulary_id = :v
"""


# =============================================================================
# Result type
# =============================================================================

@dataclass(frozen=True)
class Resolution:
    """One decision about one raw value, in the shape the ledger stores.

    Field-for-field this is ``Resolution`` in interface_contract.md, and it maps
    one-for-one onto ``fact_resolution_log``. It is frozen because a resolution
    is evidence: two calls with the same inputs must compare equal, and nothing
    downstream may edit a decision in place.

    Two things worth stating precisely, because they are contract decisions and
    not accidents:

    * ``candidates`` is JSON **text**, not a list — that is what the column holds
      and what ``schemas.ResolutionLogCreate`` validates. It is
      ``[{code, method, name, rank, score, vocab}, ...]`` in rank order, with keys
      sorted so two runs are byte-identical. ``None`` when there is nothing to
      record.
    * ``concept_code`` is ``None`` for **every** decision that did not apply a
      concept — ``unresolved``, ``rejected`` and ``ambiguous`` alike. An
      ``ambiguous`` row is a produced match whose band is not good enough to
      apply unsupervised, so the machine's leaning is recorded in ``candidates``
      and nowhere else. The guard against applying it is ``decision``, and that
      is what every consumer keys on:

          apply the concept  <=>  resolution.decision == "auto_accepted"

      An earlier revision carried the best candidate's code on an ambiguous
      resolution, on the theory that a reviewer would find it convenient. Four
      consumers disagreed: ``DBLoader.load_resolutions`` projects any non-None
      code into ``fact_concept_map``, ``schemas.ResolutionLogCreate`` rejects a
      code on a withheld decision outright, ``DataConsolidator._match_column``
      nulls it, and the golden test asserts it is absent. The producer now agrees
      with all four.
    """

    source_table: str
    source_column: str
    source_value: str
    vocabulary_id: str | None
    vocabulary_version: str | None
    concept_code: str | None
    match_method: str
    match_score: float
    decision: str
    candidates: str | None
    resolver_version: str
    run_id: str | None

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict view, for an insert or a structured log line."""
        return asdict(self)


# =============================================================================
# Internal value types
# =============================================================================

@dataclass(frozen=True)
class _Concept:
    """One row of the pack's vocabulary, as the ladder sees it."""

    code: str
    name: str
    concept_rank: int
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    """One piece of ranked evidence, before it becomes JSON."""

    vocabulary_id: str | None
    code: str
    name: str
    method: str
    score: float
    concept_rank: int


@dataclass(frozen=True, eq=False)
class _Vocabulary:
    """A pack's concepts plus the indexes the four string rungs need.

    ``eq=False`` because the mapping fields are unhashable and nothing ever needs
    value equality on this object; identity semantics keep it out of trouble.
    """

    label: str
    vocabulary_id: str | None
    domain: str | None
    case_sensitive: bool
    normalise: Callable[[str], str]
    concepts: tuple[_Concept, ...]
    names: tuple[str, ...]
    exact: Mapping[str, tuple[_Concept, ...]]
    normalised: Mapping[str, tuple[_Concept, ...]]
    alias_exact: Mapping[str, tuple[_Concept, ...]]
    alias_normalised: Mapping[str, tuple[_Concept, ...]]


# =============================================================================
# Pure module-level helpers
# =============================================================================

def _clean_optional(value: Any) -> str | None:
    """The loader's NA rule, with ``None`` instead of ``''``.

    An adapter, not a second implementation: ``_as_text`` *is*
    ``utilities.vocabulary.loader._clean_text``, which covers ``None``, ``NaN``,
    ``pd.NA`` and ``pd.NaT`` and trims. The only thing added here is the
    empty-to-``None`` conversion that ``fact_resolution_log``'s nullable columns
    want.
    """
    text_value = _as_text(value)
    return text_value or None


def _synonyms_of(extra: Mapping[str, Any]) -> tuple[str, ...]:
    """Synonyms from a **parsed** ``extra``, always a deterministic tuple.

    The loader's ``_split_synonyms`` takes a raw value (list, set or
    pipe-delimited string); this pulls the right key out of the dict first, so
    there is still exactly one splitter in the codebase.
    """
    return _split_synonyms((extra or {}).get("synonyms"))


def _require_context(context: Mapping[str, Any], name: str) -> str:
    """Read a required key out of ``context``, or explain what is missing."""
    value = _as_text(context.get(name)).strip()
    if not value:
        raise ValueError(
            f"resolve() requires context[{name!r}]. A Resolution records what "
            f"was decided about which column of which table, and both columns "
            f"are NOT NULL in fact_resolution_log, so a decision without them "
            f"cannot be recorded."
        )
    return value


def _field_of(context: Mapping[str, Any], source_table: str,
              source_column: str) -> str:
    """The field a decision is about, used as the cache key's first half.

    ``context['target_field']`` when the caller states it (``'dim_vector.
    species_name'`` — a FIELD_CATALOG name), otherwise the source pair.
    """
    target = _as_text(context.get("target_field")).strip()
    return target or f"{source_table}.{source_column}"


def _pack_vocabulary_id(pack: Any) -> str | None:
    """The version-qualified id a pack's concepts are stored under."""
    if pack is None:
        return None
    explicit = getattr(pack, "vocabulary_id", None)
    if explicit:
        return str(explicit)
    pack_id = getattr(pack, "pack_id", None)
    version = getattr(pack, "version", None)
    if pack_id and version:
        return f"{pack_id}@{version}"
    return str(pack_id) if pack_id else None


def _pack_key(pack: Any) -> str:
    """Cache key identifying one pack. '' when no pack governs the field."""
    return _pack_vocabulary_id(pack) or ""


def _pack_label(pack: Any) -> str:
    """Human-readable pack name for log lines."""
    return _pack_vocabulary_id(pack) or "no pack"


def _exact_key(value: str, case_sensitive: bool) -> str:
    """The comparison key for the exact and alias-exact indexes."""
    trimmed = value.strip()
    return trimmed if case_sensitive else trimmed.casefold()


def _ranked(candidates: Iterable[_Candidate]) -> list[_Candidate]:
    """Deduplicate by concept code, then order best-first.

    Total order: ``-score`` (highest first), ``-concept_rank`` (a vocabulary's
    own preference wins a score tie), ``code`` ascending (the final, arbitrary but
    stable breaker). No step depends on input order, so the same set of
    candidates always produces the same list.

    Deduplication keeps the highest-scoring entry per code, which matters when a
    concept's name and one of its synonyms both matched the probe.
    """
    best_by_code: dict[str, _Candidate] = {}
    for candidate in candidates:
        existing = best_by_code.get(candidate.code)
        if existing is None or candidate.score > existing.score:
            best_by_code[candidate.code] = candidate
    return sorted(
        best_by_code.values(),
        key=lambda candidate: (
            -candidate.score,
            -candidate.concept_rank,
            candidate.code,
        ),
    )


# =============================================================================
# ConceptResolver
# =============================================================================

class ConceptResolver:
    """Resolves raw CDM values against one pack at a time.

    Parameters
    ----------
    registry : VocabularyRegistry or None
        Used only when ``resolve`` is called with ``pack=None``, in which case
        the governing pack is selected by ``registry.select_pack(field, ...)``.
        ``None`` is legal as long as every call names its pack.
    session : sqlalchemy.orm.Session
        Used to read ``dim_concept``. Read-only: this class never writes.

    One instance is scoped to one run. It caches the pack's vocabulary in memory
    and memoises decisions on ``(field, value)``; both caches assume the
    vocabulary is already loaded and will not change under it.
    """

    def __init__(self, registry, session) -> None:
        self.registry = registry
        self.session = session
        self._vocabularies: dict[str, _Vocabulary] = {}
        self._resolutions: dict[tuple[str, str], Resolution] = {}
        self._warned: set[tuple[str, str]] = set()
        self._stats: Counter = Counter()

    # -------------------------------------------------------------------------
    # Public surface
    # -------------------------------------------------------------------------

    def resolve(self, value: Any, pack: Any, context: Mapping[str, Any] | None
                ) -> Resolution:
        """Resolve one raw value against one pack. Implements the ladder.

        Parameters
        ----------
        value : Any
            The raw cell. ``None``, ``NaN``, ``pd.NA`` and whitespace-only strings
            all resolve to ``unresolved`` at score 0.00 rather than raising.
        pack : Any
            The pack that governs the value's field. ``None`` defers to
            ``registry.select_pack`` using ``context['target_field']``; if no pack
            governs the field the result is ``unresolved`` with
            ``vocabulary_id=None``.
        context : mapping
            **Must carry ``source_table`` and ``source_column``.** This is a
            precondition, not a hint: both columns are NOT NULL in
            ``fact_resolution_log``, so a decision without them cannot be
            recorded, and a caller that cannot name the column it is resolving has
            a bug upstream — not a value that resolves to nothing. May also carry
            ``run_id``, ``target_field``, ``country_code`` and ``admin_level``;
            the last two are forwarded to the registry, which owns
            ``applies_when``.

        Returns
        -------
        Resolution
            Frozen, hashable, and equal for equal inputs. The second call for the
            same ``(field, value)`` returns the memoised object without touching
            the database.

        Raises
        ------
        ValueError
            If ``context`` omits ``source_table`` or ``source_column`` (or
            supplies them blank).
        FieldNotInCatalog
            If ``pack`` is None and ``context['target_field']`` is not a
            ``FIELD_CATALOG`` field. A misspelled field name is a configuration
            bug, not a legitimate "no vocabulary governs this" answer.

        Notes
        -----
        ``source_value`` is the NA-normalised, trimmed cell, because
        ``_clean_text`` is the single NA *and* whitespace rule. Trimming is the
        intended consequence: the resolver already trims its probe, so
        ``' gambiae'`` and ``'gambiae'`` never had different decisions — only
        different ledger keys, which duplicated one decision.
        """
        ctx = dict(context or {})
        source_table = _require_context(ctx, "source_table")
        source_column = _require_context(ctx, "source_column")
        run_id = _clean_optional(ctx.get("run_id"))
        field = _field_of(ctx, source_table, source_column)

        source_value = _as_text(value)

        cache_key = (field, source_value)
        memoised = self._resolutions.get(cache_key)
        if memoised is not None:
            self._stats["cache_hit"] += 1
            logger.debug(
                "cache hit for %s = %r (%s)", field, source_value,
                memoised.decision,
            )
            return memoised

        governing = pack
        if governing is None:
            governing = self._select_pack(field, source_value, ctx)

        resolution = self._resolve_ladder(
            raw=source_value,
            pack=governing,
            source_table=source_table,
            source_column=source_column,
            source_value=source_value,
            run_id=run_id,
            field=field,
        )

        self._resolutions[cache_key] = resolution
        self._stats["resolved"] += 1
        self._stats[f"decision.{resolution.decision}"] += 1
        self._stats[f"method.{resolution.match_method}"] += 1
        return resolution

    def resolve_batch(self, values: Iterable[Any], pack: Any,
                      context: Mapping[str, Any] | None) -> list[Resolution]:
        """Resolve many values, one Resolution per distinct value.

        Duplicates collapse: the ledger's unique key is
        ``(source_table, source_column, source_value, run_id)``, so a second
        Resolution for the same value would only duplicate a decision. Order is
        first appearance, which is stable for an ordered source and irrelevant for
        an unordered one (the ledger sorts nothing).
        """
        ctx = dict(context or {})
        source_table = _require_context(ctx, "source_table")
        source_column = _require_context(ctx, "source_column")
        field = _field_of(ctx, source_table, source_column)

        seen: set[tuple[str, str]] = set()
        results: list[Resolution] = []
        collapsed = 0

        for value in values:
            key = (field, _as_text(value))
            if key in seen:
                collapsed += 1
                continue
            seen.add(key)
            results.append(self.resolve(value, pack, ctx))

        counts = Counter(resolution.decision for resolution in results)
        logger.info(
            "resolved %d distinct value(s) for %s.%s against %s: %s"
            + (f" ({collapsed} duplicate value(s) collapsed)" if collapsed else ""),
            len(results), source_table, source_column, _pack_label(pack),
            ", ".join(f"{name}={counts[name]}" for name in sorted(counts)) or "none",
        )
        return results

    def stats(self) -> dict[str, Any]:
        """A deterministic snapshot of what this instance has done."""
        summary: dict[str, Any] = {
            name: self._stats[name] for name in sorted(self._stats)
        }
        summary["vocabularies_loaded"] = len(self._vocabularies)
        summary["resolutions_cached"] = len(self._resolutions)
        summary["resolver_version"] = RESOLVER_VERSION
        summary["fuzzy_library"] = FUZZY_LIBRARY_VERSION
        return summary

    @staticmethod
    def _apply_margin(best: float, runner_up: float | None, pack: Any) -> bool:
        """Whether ``best`` clears the pack's margin over ``runner_up``.

        Pure: reads only its arguments, so it can be tested with the literal
        numbers from the review (``best=0.96``, ``runner_up=0.95``).

        ``runner_up is None`` means the vocabulary holds a single concept, so
        there is nothing to be confused with and the margin is trivially
        satisfied. It is deliberately not ``0.0``: a one-concept vocabulary would
        then fail every non-zero margin and no value would ever resolve.

        ``margin == 0`` accepts an exact tie. That is the pack's declaration, and
        the tie is still broken deterministically by ``(concept_rank,
        concept_code)``; a pack that wants ties flagged sets a non-zero margin.
        """
        margin = float(getattr(pack, "fuzzy_margin"))
        if runner_up is None:
            return True
        return (float(best) - float(runner_up)) >= margin

    # -------------------------------------------------------------------------
    # Pack selection
    # -------------------------------------------------------------------------

    def _select_pack(self, field: str, value: str,
                     context: Mapping[str, Any]) -> Any:
        """Ask the registry which pack governs ``field``.

        ``FieldNotInCatalog`` propagates: an unknown field is a typo, and the
        registry documents that distinction.
        """
        if self.registry is None:
            raise ValueError(
                "resolve() was called with pack=None and no registry was "
                "supplied to ConceptResolver. Pass a pack, or construct the "
                "resolver with a VocabularyRegistry."
            )
        return self.registry.select_pack(field, sample=value, context=context)

    # -------------------------------------------------------------------------
    # The ladder
    # -------------------------------------------------------------------------

    def _resolve_ladder(self, *, raw: str, pack: Any, source_table: str,
                        source_column: str, source_value: str,
                        run_id: str | None, field: str) -> Resolution:
        """Walk the six rungs in contract order. First match decides."""
        base = {
            "source_table": source_table,
            "source_column": source_column,
            "source_value": source_value,
            "vocabulary_id": _pack_vocabulary_id(pack),
            "vocabulary_version": _clean_optional(getattr(pack, "version", None)),
            "resolver_version": RESOLVER_VERSION,
            "run_id": run_id,
        }

        def _unresolved(method: str = METHOD_UNRESOLVED,
                        score: float = UNRESOLVED_SCORE,
                        candidates: str | None = None) -> Resolution:
            return Resolution(
                concept_code=None,
                match_method=method,
                match_score=score,
                decision=UNRESOLVED,
                candidates=candidates,
                **base,
            )

        if pack is None:
            logger.debug(
                "no pack governs %s; recording the value as unresolved", field,
            )
            return _unresolved()

        probe = raw.strip()
        if not probe:
            logger.debug("empty value for %s; recording it as unresolved", field)
            return _unresolved()

        vocabulary = self._vocabulary(pack)
        if not vocabulary.concepts:
            self._warn_empty(pack, vocabulary)
            return _unresolved()

        # ---- L1 exact ---------------------------------------------------
        matches = vocabulary.exact.get(
            _exact_key(probe, vocabulary.case_sensitive), ()
        )
        if matches:
            return self._from_string_rung(
                matches, EXACT_SCORE, METHOD_EXACT, vocabulary, base
            )

        # ---- L2 normalized ----------------------------------------------
        normalised_probe = self._normalise_key(
            vocabulary.normalise, probe, vocabulary.label
        )
        if normalised_probe:
            matches = vocabulary.normalised.get(normalised_probe, ())
            if matches:
                return self._from_string_rung(
                    matches, NORMALIZED_SCORE, METHOD_NORMALIZED, vocabulary, base
                )

        # ---- L3 alias ---------------------------------------------------
        alias_matches = list(
            vocabulary.alias_exact.get(
                _exact_key(probe, vocabulary.case_sensitive), ()
            )
        )
        if normalised_probe:
            alias_matches.extend(
                vocabulary.alias_normalised.get(normalised_probe, ())
            )
        if alias_matches:
            return self._from_string_rung(
                alias_matches, ALIAS_SCORE, METHOD_ALIAS, vocabulary, base
            )

        # ---- L4 fuzzy (threshold AND margin) ----------------------------
        best, runner_up, ranked = self._fuzzy(vocabulary, probe, pack)
        if best is None:
            logger.debug(
                "no rung matched %r for %s against %s (best candidate %s)",
                probe, field, vocabulary.label,
                "n/a" if not ranked else f"{ranked[0].code} at {ranked[0].score:.3f}",
            )
            return _unresolved(
                candidates=self._record_candidates(ranked) if ranked else None
            )

        accepted, decision, method = self._fuzzy_outcome(best, runner_up, pack)
        if not accepted:
            # ---- L5 ambiguous -------------------------------------------
            #
            # `concept_code` is None by contract: this is a produced match the
            # margin rule withheld, so the reviewer sees the leaning in
            # `candidates` and no consumer can mistake it for an applied concept.
            logger.debug(
                "ambiguous %r for %s against %s: best %s at %.3f, runner-up %s "
                "at %s, margin %.3f",
                probe, field, vocabulary.label, best.code, best.score,
                runner_up.code if runner_up else "n/a",
                f"{runner_up.score:.3f}" if runner_up else "n/a",
                float(getattr(pack, "fuzzy_margin")),
            )
            return Resolution(
                concept_code=None,
                match_method=method,
                match_score=best.score,
                decision=decision,
                candidates=self._record_candidates(ranked),
                **base,
            )

        return Resolution(
            concept_code=best.code,
            match_method=method,
            match_score=best.score,
            decision=decision,
            candidates=self._record_candidates(ranked),
            **base,
        )

    def _from_string_rung(self, matches: Sequence[_Concept], score: float,
                          method: str, vocabulary: _Vocabulary,
                          base: Mapping[str, Any]) -> Resolution:
        """Build the Resolution for a rung that matched by string equality.

        Several concepts can share a name; the winner is chosen by
        ``(concept_rank, concept_code)``, not by row order. A string rung is
        therefore never ``ambiguous``: ambiguity in the sense of §3 step 5 is a
        *fuzzy* band question, and an exact hit on two identically named concepts
        is a vocabulary defect the rank field is designed to settle.
        """
        candidates = _ranked(
            _Candidate(
                vocabulary_id=vocabulary.vocabulary_id,
                code=concept.code,
                name=concept.name,
                method=method,
                score=score,
                concept_rank=concept.concept_rank,
            )
            for concept in matches
        )
        best = candidates[0]
        return Resolution(
            concept_code=best.code,
            match_method=method,
            match_score=score,
            decision=AUTO_ACCEPTED,
            candidates=self._record_candidates(candidates),
            **base,
        )

    def _fuzzy_outcome(self, best: _Candidate, runner_up: _Candidate | None,
                       pack: Any) -> tuple[bool, str, str]:
        """Apply the margin rule and translate it into (accepted, decision, method).

        Extracted so the review's literal fixture — ``best=0.96``,
        ``runner_up=0.95``, margin ``0.05`` must be ambiguous — can be asserted
        through the real decision path rather than only against the comparator.
        """
        if self._apply_margin(
            best.score, runner_up.score if runner_up else None, pack
        ):
            return True, AUTO_ACCEPTED, METHOD_FUZZY
        return False, NEEDS_REVIEW, METHOD_AMBIGUOUS

    # -------------------------------------------------------------------------
    # Fuzzy rung
    # -------------------------------------------------------------------------

    def _fuzzy(self, vocabulary: _Vocabulary, probe: str, pack: Any
               ) -> tuple[_Candidate | None, _Candidate | None, list[_Candidate]]:
        """Score every concept name against ``probe``.

        Returns ``(best, runner_up, ranked)``, where ``best`` is None unless the
        top score clears ``pack.fuzzy_threshold``.

        The search is bounded by ``score_cutoff = (threshold - margin) * 100``.
        That bound is exact, not an optimisation that could change a decision:

        * a candidate *below* the threshold can never be the best, because the
          best must clear it;
        * a candidate scoring at or below ``threshold - margin`` can never make
          the margin fail, because ``best >= threshold`` implies
          ``best - candidate >= margin`` already.

        Everything above the bound is returned, so the true runner-up is always
        among the hits — which matters, because a naive ``limit=2`` on an uncapped
        search, or a cutoff at the threshold itself, would hide a runner-up that
        is unhelpfully close and turn an ambiguous value into a silent
        auto-accept.

        Ties are broken by ``_ranked`` after the search, never by RapidFuzz's own
        result order.
        """
        if not vocabulary.names:
            return None, None, []

        threshold = float(getattr(pack, "fuzzy_threshold"))
        margin = float(getattr(pack, "fuzzy_margin"))

        cutoff = (threshold - margin) * 100.0 - _CUTOFF_TOLERANCE
        if cutoff < 0.0:
            cutoff = 0.0

        hits = process.extract(
            probe,
            vocabulary.names,
            scorer=fuzz.token_set_ratio,
            limit=len(vocabulary.names),
            score_cutoff=cutoff,
        )

        candidates: list[_Candidate] = []
        for hit in hits:
            try:
                _name = hit[0]
                ratio = float(hit[1])
                position = int(hit[2])
            except (IndexError, TypeError, ValueError):
                # extract() returns (choice, score, index) for a sequence of
                # strings; anything else is a library change we must not guess at.
                logger.error(
                    "unexpected rapidfuzz.process.extract result %r (%s); "
                    "skipping it. Pin rapidfuzz==%s.",
                    hit, type(hit).__name__, rapidfuzz.__version__,
                )
                continue
            if position < 0 or position >= len(vocabulary.concepts):
                continue

            concept = vocabulary.concepts[position]
            score = ratio / 100.0
            if score > 1.0:
                score = 1.0
            elif score < 0.0:
                score = 0.0

            candidates.append(
                _Candidate(
                    vocabulary_id=vocabulary.vocabulary_id,
                    code=concept.code,
                    name=concept.name,
                    method=METHOD_FUZZY,
                    score=score,
                    concept_rank=concept.concept_rank,
                )
            )

        ranked = _ranked(candidates)
        if not ranked:
            return None, None, []

        best = ranked[0]
        if best.score < threshold:
            return None, None, ranked

        runner_up = ranked[1] if len(ranked) > 1 else None
        return best, runner_up, ranked

    # -------------------------------------------------------------------------
    # Evidence
    # -------------------------------------------------------------------------

    def _record_candidates(self, best_k: Sequence[_Candidate]) -> str | None:
        """Serialise the top candidates as the ledger's JSON evidence.

        Ordered ``(-score, -concept_rank, concept_code)`` — the contract's
        ``(score, concept_rank, concept_code)`` with the two descending terms
        negated so "best first" falls out of an ascending sort. Re-sorted here
        even when the caller already sorted, because this is the method that
        writes the evidence and the ordering guarantee belongs to it.

        Floats are rounded to six places in the JSON only. The exact ratio stays
        on ``Resolution.match_score``; rounding in the evidence keeps two runs
        byte-identical when a library returns, say, 95.00000000000001.
        """
        if not best_k:
            return None

        ordered = sorted(
            best_k,
            key=lambda candidate: (
                -candidate.score,
                -candidate.concept_rank,
                candidate.code,
            ),
        )[:DEFAULT_CANDIDATE_LIMIT]

        payload = [
            {
                "vocab": candidate.vocabulary_id,
                "code": candidate.code,
                "name": candidate.name,
                "method": candidate.method,
                "score": round(candidate.score, SCORE_PRECISION),
                "rank": candidate.concept_rank,
            }
            for candidate in ordered
        ]
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    # -------------------------------------------------------------------------
    # Vocabulary loading (cached)
    # -------------------------------------------------------------------------

    def _vocabulary(self, pack: Any) -> _Vocabulary:
        """The pack's concepts and indexes, read once per resolver instance."""
        key = _pack_key(pack)
        cached = self._vocabularies.get(key)
        if cached is not None:
            return cached
        vocabulary = self._load_vocabulary(pack)
        self._vocabularies[key] = vocabulary
        return vocabulary

    def _load_vocabulary(self, pack: Any) -> _Vocabulary:
        """Query ``dim_concept`` and build the four string indexes.

        The query is ``ORDER BY concept_code`` (interface contract). Every index
        bucket therefore inherits concept-code order, and ``_ranked`` preserves it
        whenever scores and ranks tie.
        """
        vocabulary_id = _pack_vocabulary_id(pack)
        domain = _clean_optional(getattr(pack, "domain", None))
        case_sensitive = bool(getattr(pack, "case_sensitive", False))
        label = _pack_label(pack)

        where = "vocabulary_id = :v"
        params: dict[str, Any] = {"v": vocabulary_id}
        if domain:
            where += " AND domain = :d"
            params["d"] = domain

        rows = self.session.execute(
            text(_CONCEPT_QUERY.format(where=where)), params
        ).fetchall()

        normalise = pack.normalizer()

        concepts: list[_Concept] = []
        names: list[str] = []
        exact: dict[str, list[_Concept]] = {}
        normalised: dict[str, list[_Concept]] = {}
        alias_exact: dict[str, list[_Concept]] = {}
        alias_normalised: dict[str, list[_Concept]] = {}

        for row in rows:
            code = _as_text(row[0]).strip()
            name = _as_text(row[1]).strip()
            if not code or not name:
                continue

            extra = _parse_extra(row[2])
            concept = _Concept(
                code=code,
                name=name,
                concept_rank=_rank_of(extra),
                synonyms=_synonyms_of(extra),
            )
            concepts.append(concept)
            names.append(name)

            exact.setdefault(_exact_key(name, case_sensitive), []).append(concept)

            name_key = self._normalise_key(normalise, name, label)
            if name_key:
                normalised.setdefault(name_key, []).append(concept)

            for synonym in concept.synonyms:
                alias_exact.setdefault(
                    _exact_key(synonym, case_sensitive), []
                ).append(concept)
                synonym_key = self._normalise_key(normalise, synonym, label)
                if synonym_key:
                    alias_normalised.setdefault(synonym_key, []).append(concept)

        logger.info(
            "loaded %d concept(s) for %s (domain %s, %d normalised key(s), "
            "%d synonym key(s))",
            len(concepts), label, domain or "(any)",
            len(normalised), len(alias_exact) + len(alias_normalised),
        )

        return _Vocabulary(
            label=label,
            vocabulary_id=vocabulary_id,
            domain=domain,
            case_sensitive=case_sensitive,
            normalise=normalise,
            concepts=tuple(concepts),
            names=tuple(names),
            exact={key: tuple(value) for key, value in exact.items()},
            normalised={key: tuple(value) for key, value in normalised.items()},
            alias_exact={key: tuple(value) for key, value in alias_exact.items()},
            alias_normalised={
                key: tuple(value) for key, value in alias_normalised.items()
            },
        )

    def _normalise_key(self, normalise: Callable[[str], str], value: str,
                       label: str) -> str:
        """Apply a pack's normaliser, surviving a normaliser that raises.

        A normaliser is a pure ``str -> str`` and should not raise, so an
        exception here is a defect — but one that must not take a whole run down.
        Degrading that key to "" disables the normalised and alias-normalised
        rungs for this vocabulary while leaving exact and fuzzy matching working,
        and the ERROR line names the pack and the exception.
        """
        try:
            return normalise(value)
        except Exception as exc:  # noqa: BLE001 - a normaliser must not kill a run
            marker = ("normalizer", label)
            if marker not in self._warned:
                self._warned.add(marker)
                logger.error(
                    "the normaliser for pack %r raised %s: %s. The normalised "
                    "and alias rungs are disabled for this pack; exact and "
                    "fuzzy matching continue. Fix the normaliser in "
                    "utilities/vocabulary/normalizers.py.",
                    label, type(exc).__name__, exc,
                )
            return ""

    # -------------------------------------------------------------------------
    # Diagnostics
    # -------------------------------------------------------------------------

    def _warn_empty(self, pack: Any, vocabulary: _Vocabulary) -> None:
        """Explain an empty vocabulary once, with the two likely causes.

        An unloaded or misnamed vocabulary used to resolve every value to
        nothing, silently. The two causes seen in practice are a domain that
        disagrees with the pack's (so the filter excluded every row) and a
        vocabulary loaded under an unversioned id (so ``'gadm@4.1'`` matches
        nothing while ``'GADM'`` holds the rows). Both are named here, with the
        counts that prove it.
        """
        marker = ("empty", vocabulary.label)
        if marker in self._warned:
            return
        self._warned.add(marker)

        vocabulary_id = vocabulary.vocabulary_id
        detail = ""

        try:
            breakdown = self.session.execute(
                text(_DOMAIN_BREAKDOWN_QUERY), {"v": vocabulary_id}
            ).fetchall()
            if breakdown:
                detail = " domains present for this vocabulary: " + ", ".join(
                    f"{_as_text(row[0]) or '(null)'}={int(row[1])}"
                    for row in breakdown
                ) + "."
        except Exception as exc:  # diagnostics must never mask the real problem
            logger.debug("domain breakdown unavailable: %s", exc)

        if not detail and vocabulary_id and "@" in vocabulary_id:
            base = vocabulary_id.split("@", 1)[0]
            try:
                legacy = self.session.execute(
                    text(_LEGACY_ID_QUERY), {"v": base}
                ).scalar()
            except Exception as exc:
                logger.debug("legacy-id check unavailable: %s", exc)
                legacy = None
            if legacy:
                detail = (
                    f" {int(legacy)} concept(s) are stored under the unversioned "
                    f"id {base!r}; the resolver only reads "
                    f"{vocabulary_id!r}, so they are invisible. Re-load that "
                    f"vocabulary with the pack, via "
                    f"VocabularyLoader.load_csv(csv, registry.get(pack_id))."
                )

        logger.warning(
            "pack %r (domain %s) has no concepts to match against; every value "
            "resolves to 'unresolved'.%s",
            vocabulary.label, vocabulary.domain or "(any)", detail,
        )


# =============================================================================
# Convenience
# =============================================================================

def resolve_value(registry, session, value: Any, target_field: str, *,
                  source_table: str, source_column: str,
                  run_id: str | None = None) -> Resolution:
    """Resolve one value with a one-shot resolver. For callers outside a run.

    A batch pipeline should build one ``ConceptResolver`` per run and call
    ``resolve`` or ``resolve_batch``; this helper exists so a CLI, a test or a
    single repair has a one-liner that still records the full field context.
    """
    resolver = ConceptResolver(registry, session)
    return resolver.resolve(
        value,
        pack=None,
        context={
            "source_table": source_table,
            "source_column": source_column,
            "target_field": target_field,
            "run_id": run_id,
        },
    )


__all__ = [
    "ALIAS_SCORE",
    "AUTO_ACCEPTED",
    "ConceptResolver",
    "DECISIONS",
    "DEFAULT_CANDIDATE_LIMIT",
    "EXACT_SCORE",
    "FUZZY_LIBRARY_VERSION",
    "METHOD_ALIAS",
    "METHOD_AMBIGUOUS",
    "METHOD_EXACT",
    "METHOD_FUZZY",
    "METHOD_NORMALIZED",
    "METHOD_UNRESOLVED",
    "NEEDS_REVIEW",
    "NORMALIZED_SCORE",
    "REJECTED",
    "RESISTANCE_BANDS",
    "RESOLVER_VERSION",
    "Resolution",
    "UNRESOLVED",
    "UNRESOLVED_SCORE",
    "resolve_value",
]