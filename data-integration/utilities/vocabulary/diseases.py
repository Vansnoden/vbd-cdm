"""
Extract disease and malaria terms from IDOMAL (OBO format).

IDOMAL is the malaria ontology, built on BFO and following OBO Foundry rules.
Download: http://purl.obolibrary.org/obo/idomal.obo
"""

import pronto
import pandas as pd
from typing import Optional
from typing import Any, Dict, List, Optional
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def build_disease_vocabulary(
    obo_path: Any,
    output_path: Any,
    *,
    vocabulary_id: str,
    ontology_prefix: Optional[str] = None,
    include_obsolete: bool = False,
    max_synonyms: int = 8,
) -> "pd.DataFrame":
    """Build the disease vocabulary CSV from an OBO/OWL ontology.

    Every row's ``vocabulary_id`` comes from ``vocabulary_id`` via
    :func:`_detect_vocab_id`, never from ``obo_path``. That is the whole point of
    the signature: the file tells you what to parse, the caller tells you what
    the concepts *are*, and the two are not allowed to be confused.

    Parameters
    ----------
    obo_path : str or pathlib.Path
        The ontology file. Any format ``pronto`` can open.
    output_path : str or pathlib.Path
        Where the CSV is written. Parent directories are created.
    vocabulary_id : str
        Keyword-only and required. The identity every row is stamped with, e.g.
        ``'IDOMAL'``. Must not look like a path.
    ontology_prefix : str, optional
        Keep only concepts whose code starts with this prefix (``'IDOMAL:'``).
        ``None`` keeps every term, which is right for a single-disease ontology
        and wrong for a multi-species one.
    include_obsolete : bool
        Keep terms flagged obsolete in the source. Off by default: an obsolete
        term must not be matched against a live record.
    max_synonyms : int
        Cap on the synonym list per concept, so one verbose term cannot produce a
        megabyte-wide CSV cell. Order is the ontology's own declaration order,
        preserved, so the file is reproducible.

    Returns
    -------
    pandas.DataFrame
        The frame written, with columns ``vocabulary_id``, ``concept_code``,
        ``concept_name``, ``concept_class``, ``parent_code``, ``definition``,
        ``synonyms`` (pipe-joined) — the shape
        ``VocabularyLoader.load_csv`` reads.

    Raises
    ------
    ValueError
        If the identity is absent or path-like, if the file produces no usable
        concept, or if the result contains a duplicate ``concept_code``.

    Notes
    -----
    An empty build raises rather than writing a header-only CSV. A vocabulary
    with no concepts resolves nothing, and the failure mode is a run that reports
    success while standardising nothing — review C10, one layer up.
    """
    identity = _detect_vocab_id(vocabulary_id)

    source = Path(obo_path)
    if not source.is_file():
        raise FileNotFoundError(f"ontology file not found: {source}")

    prefix = None if ontology_prefix is None else str(ontology_prefix).strip()
    if prefix == "":
        prefix = None

    def _identifier(term: Any) -> str:
        """``term.id`` as a clean string; '' when the term has none."""
        raw = getattr(term, "id", None)
        if raw is None:
            return ""
        return str(raw).strip()

    def _definition(term: Any) -> str:
        """The term's first definition, stripped, or ''."""
        raw = getattr(term, "definition", None)
        if raw is None:
            return ""
        text_value = str(raw).strip()
        return text_value

    def _synonyms(term: Any) -> List[str]:
        """The term's synonyms as declarative strings, de-duplicated, capped.

        ``pronto`` yields ``Synonym`` objects whose ``str()`` is the declarative
        form (``"malaria" EXACT []``); only the quoted label is wanted, so a
        leading/trailing quote and any trailing qualifier are removed. Order is
        the ontology's, never a set's, so two builds produce the same CSV.
        """
        seen: List[str] = []
        try:
            declared = list(getattr(term, "synonyms", ()) or ())
        except Exception:  # noqa: BLE001 - a broken synonym must not lose a term
            return seen

        for synonym in declared:
            text_value = str(synonym).strip()
            if not text_value:
                continue
            if text_value[0] == '"':
                closing = text_value.find('"', 1)
                text_value = (
                    text_value[1:closing] if closing > 0 else text_value[1:]
                )
            text_value = text_value.strip()
            if text_value and text_value not in seen:
                seen.append(text_value)
            if len(seen) >= max(0, int(max_synonyms)):
                break
        return seen

    ontology = pronto.Ontology(str(source))

    rows: List[Dict[str, Any]] = []
    skipped_obsolete = 0
    skipped_prefix = 0
    skipped_incomplete = 0

    for term in ontology.terms():
        if not include_obsolete and bool(getattr(term, "obsolete", False)):
            skipped_obsolete += 1
            continue

        code = _identifier(term)
        name = str(getattr(term, "name", "") or "").strip()
        if not code or not name:
            skipped_incomplete += 1
            continue

        if prefix is not None and not code.startswith(prefix):
            skipped_prefix += 1
            continue

        parents = sorted(
            str(parent).strip()
            for parent in (getattr(term, "parents", ()) or ())
            if str(parent).strip()
        )

        rows.append({
            "vocabulary_id": identity,
            "concept_code": code,
            "concept_name": name,
            "concept_class": (str(getattr(term, "namespace", "") or "").strip() or None),
            "parent_code": parents[0] if parents else None,
            "definition": _definition(term) or None,
            "synonyms": "|".join(_synonyms(term)) or None,
        })

    if not rows:
        raise ValueError(
            f"{source}: produced no usable concepts (skipped "
            f"{skipped_obsolete} obsolete, {skipped_prefix} outside prefix "
            f"{prefix!r}, {skipped_incomplete} without a code or a name). Writing "
            f"an empty vocabulary would standardise every value to 'unresolved' "
            f"and report success; fix the source or the prefix instead."
        )

    frame = pd.DataFrame(
        rows,
        columns=[
            "vocabulary_id", "concept_code", "concept_name", "concept_class",
            "parent_code", "definition", "synonyms",
        ],
    )

    duplicated = frame["concept_code"][frame["concept_code"].duplicated()].unique()
    if len(duplicated):
        raise ValueError(
            f"{source}: {len(duplicated)} concept code(s) appear more than once "
            f"(e.g. {sorted(duplicated)[:5]}). dim_concept keys on "
            f"(vocabulary_id, concept_code), so a duplicate would silently "
            f"overwrite a concept on load."
        )

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False, lineterminator="\n")

    logger.info(
        "built disease vocabulary %r from %s: %d concept(s) -> %s "
        "(skipped %d obsolete, %d outside prefix, %d incomplete)",
        identity, source.name, len(frame), destination,
        skipped_obsolete, skipped_prefix, skipped_incomplete,
    )
    return frame
    

def _infer_class(term) -> str:
    """Crude class inference from the term's ancestor chain."""
    ancestors = {a.name.lower() for a in term.superclasses(distance=100) if a.name}
    if "disease" in ancestors:
        return "disease"
    if "mosquito" in ancestors or "vector" in ancestors:
        return "vector"
    if "insecticide" in ancestors:
        return "insecticide"
    return "term"


def _detect_vocab_id(vocab_id: Optional[str] = None) -> str:
    """Return the vocabulary identity the caller declares, or raise.

    Review C11: this function used to infer identity from a filename substring
    (``"idomal" in path.lower() -> "IDOMAL"``). Building from ``mondo.obo``
    therefore stamped MONDO codes on rows that ``standardize_vocabularies`` then
    loaded under the id ``"IDOMAL"`` — two vocabularies silently became one, and
    the only way to detect it was to read the CSV. Identity from a path is
    precisely the implicit coupling invariant 1 forbids.

    There is no inference here and no default. The caller (``build.py``) owns the
    identity string, because it is the only place that knows which release the
    operator asked for.

    A path is rejected explicitly rather than silently accepted. That is not
    inference: it is a guard against the one mistake this refactor invites —
    forwarding ``obo_path`` where an id now belongs, which would otherwise write
    a full filesystem path into every ``vocabulary_id`` and every FK that points
    at it.

    Parameters
    ----------
    vocab_id : str
        The vocabulary identity, e.g. ``'IDOMAL'``, ``'MONDO'``, ``'IDO'``.

    Returns
    -------
    str
        The identity, stripped, otherwise unchanged. Case is preserved: the
        caller decides how the id is spelled, and
        ``dim_vocabulary.vocabulary_id`` is compared literally.

    Raises
    ------
    ValueError
        If no id was supplied, or if the supplied value looks like a path.
    """
    if vocab_id is None:
        raise ValueError(
            "no vocabulary_id supplied. _detect_vocab_id no longer infers the "
            "identity from a filename (review C11): pass the id the ontology "
            "should be recorded under, e.g. _detect_vocab_id('IDOMAL')."
        )

    identity = str(vocab_id).strip()
    if not identity:
        raise ValueError(
            "vocabulary_id is empty. Pass the id the ontology should be "
            "recorded under, e.g. _detect_vocab_id('IDOMAL')."
        )

    lowered = identity.lower()
    looks_like_path = (
        "/" in identity
        or "\\" in identity
        or lowered.endswith((".obo", ".owl", ".csv", ".tsv", ".json", ".yaml", ".yml"))
    )
    if looks_like_path:
        raise ValueError(
            f"vocabulary_id {identity!r} looks like a path, not an identity. "
            f"Pass the vocabulary id (e.g. 'IDOMAL'); the build script knows "
            f"which file it is reading, and a path in dim_vocabulary would be "
            f"propagated into every row that references the vocabulary."
        )

    return identity
