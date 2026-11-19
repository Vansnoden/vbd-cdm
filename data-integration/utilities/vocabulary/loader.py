# path: utilities/vocabulary/loader.py
# status: FULL FILE REPLACEMENT
# -*- coding: utf-8 -*-
"""
Vocabulary ingestion: one vocabulary release, loaded once, verifiably.

What this module used to be, and what it is now
-----------------------------------------------
It used to be both a loader and a matcher: ``load_csv`` wrote ``version=None``
(review C5) and dropped ``centroid_lat`` / ``centroid_lon`` / ``definition``
(C14); ``_get`` ran an unordered ``SELECT`` so two databases could return
different codes for one raw value (C9); ``match`` was a second, parallel
matcher with its own ladder, its own thresholds and a ``domain == "vector"``
special case (C8). It also contained ``_get_species_index``, and — because batch
2 appended its revisions *below* the originals — two definitions of each of
``load_csv``, ``_get`` and ``match``, where the originals shadowed the revisions
(§6.1). That last defect is the reason this batch is a full-file replacement.

The module is now ingestion only:

* :meth:`VocabularyLoader.load_csv` takes a ``ConceptPack`` and writes every
  column the pack declares — ``version``, ``pack_id``, ``released_with``,
  ``checksum``, ``concept_norm``, and an ``extra`` JSON blob carrying
  synonyms, definition and centroid — with one ``executemany``.
* :meth:`VocabularyLoader._get` is a read-only inspection helper with
  ``ORDER BY concept_code``, and nothing in the resolution path calls it.
* ``match``, its ladder helpers and ``_get_species_index`` are **gone**.
  ``etl.transformers.concept_resolver.ConceptResolver`` is the only matcher
  (contract Amendment). There is deliberately no stub: a stub would keep a
  caller alive that must be migrated in batch 6D, and a silent
  ``NotImplementedError`` is worse than an ``AttributeError`` at import.

The four parsing helpers
------------------------
``_parse_extra``, ``_split_synonyms``, ``_clean_text`` and ``_as_float`` are
defined here once and exported. They were duplicated across ``loader.py``,
``concept_resolver.py``, ``dimension_index.py`` and ``admin_alignment.py``
(review §6.2 / §6.4), with inconsistent ``pd.NA`` handling. New code imports
them from here; the two remaining copies in ``concept_resolver`` are removed in
batch 6B, and until then that module's docstring says why.

Module-level assumptions
------------------------
* ``pandas`` is a hard dependency (CSV reading, NaN handling).
* ``sqlalchemy`` 2.x — raw SQL through ``text()``; ``inspect()`` is used only
  to probe whether an optional column exists.
* The session handed to the loader is bound to a database whose schema includes
  ``dim_vocabulary`` and ``dim_concept`` with the columns the models declare.
  ``dim_vocabulary.extra`` is *optional*: it is written when present and the
  absence is logged (see :func:`dim_vocabulary_supports_extra`) because
  ``database/models.py`` is out of this batch's scope.
* ``ConceptPack`` is duck-typed. This module never imports
  ``utilities.vocabulary.registry`` at runtime, so the import graph stays
  acyclic and a test can use a five-attribute stand-in.
* ``normalizers`` is imported as a module and normalisers are addressed *by id*
  from its ``NORMALIZERS`` registry. ``_norm_species`` / ``_norm_deep`` are
  **not** defined here any more (review §6.3).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence

import pandas as pd
from sqlalchemy import inspect, text
from typing import Any, Dict, List, Optional

from utilities.vocabulary import normalizers

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from utilities.vocabulary.registry import ConceptPack

logger = logging.getLogger(__name__)


# =============================================================================
# dim_vocabulary.checksum — the artefact discriminator (contract Amendment §5.4)
#
# One column carries two different hashes: the registry hashes a pack's
# *manifest*, the loader hashes a vocabulary *CSV*. A reader cannot tell which
# from a 64-character hex string, so every write also records which artefact
# the digest describes. `extra` is where the contract puts that discriminator.
# =============================================================================

#: ``dim_vocabulary.checksum`` holds the SHA-256 of the pack's manifest bytes.
CHECKSUM_ARTEFACT_MANIFEST = "manifest"

#: ``dim_vocabulary.checksum`` holds the SHA-256 of the vocabulary CSV bytes.
CHECKSUM_ARTEFACT_CSV = "csv"


def extra_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON text for a ``dim_*`` ``extra`` column.

    ``sort_keys`` and fixed separators so two runs produce byte-identical text;
    ``ensure_ascii`` left on so the text is portable and comparable.
    """
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))


def checksum_artefact_json(artefact: str) -> str:
    """``{"checksum_artefact": <artefact>}`` as canonical JSON text."""
    return extra_json({"checksum_artefact": str(artefact)})


_EXTRA_SUPPORT_CACHE: dict[str, bool] = {}
_EXTRA_SUPPORT_WARNED: set[str] = set()


def dim_vocabulary_supports_extra(session) -> bool:
    """Whether ``dim_vocabulary`` carries the ``extra`` JSON column.

    The contract (Amendment §5.4) puts the checksum discriminator in
    ``DimVocabulary.extra``. That column is created by batch 6B, which owns
    ``database/models.py`` and its migration; this batch owns the *writers*.
    Rather than write SQL that fails on today's schema, or silently drop the
    discriminator, the writers probe once and:

    * when the column exists, write it;
    * when it does not, log a WARNING naming the consequence — a bare
      ``checksum`` whose artefact is unknowable — and continue, so ingestion
      still runs on an un-migrated database.

    Returns ``False`` when the probe cannot run at all (no bind, no driver),
    which is the same situation as "column absent": write less, say so.
    """
    try:
        bind = session.get_bind()
    except Exception as exc:  # noqa: BLE001 - a probe must never break a load
        logger.debug("cannot inspect the session bind: %s", exc)
        return False

    database = getattr(getattr(bind, "url", None), "database", None) or ""
    cache_key = f"{getattr(bind.dialect, 'name', '?')}:{database}"

    cached = _EXTRA_SUPPORT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        columns = {column["name"] for column in inspect(bind).get_columns("dim_vocabulary")}
        supported = "extra" in columns
    except Exception as exc:  # noqa: BLE001
        logger.debug("cannot inspect dim_vocabulary columns: %s", exc)
        supported = False

    _EXTRA_SUPPORT_CACHE[cache_key] = supported

    if not supported and cache_key not in _EXTRA_SUPPORT_WARNED:
        _EXTRA_SUPPORT_WARNED.add(cache_key)
        logger.warning(
            "dim_vocabulary has no `extra` column, so checksum writes cannot "
            "record which artefact they describe (contract Amendment §5.4 "
            "requires {\"checksum_artefact\": \"manifest\"|\"csv\"}). Ingestion "
            "continues; every checksum written to this database is ambiguous "
            "until the column is added by the batch that owns "
            "database/models.py.",
        )

    return supported


# =============================================================================
# The four consolidated parsing helpers
#
# Single definition. `dim_concept.extra` has been a JSON blob since batch 1 and
# a pipe-delimited synonym string before that, so every reader has to accept
# both shapes; having four slightly different readers is how a `pd.NA` became
# the literal string "<NA>" in one code path and "" in another (review §6.4).
# =============================================================================

_NA_TYPE_NAMES = frozenset({"NAType", "NaTType"})


def _clean_text(value: Any) -> str:
    """``str(value).strip()`` with ``None``/NaN/``pd.NA`` mapped to ``''``.

    Never raises. Covers the four spellings of "no value" that reach a
    vocabulary frame: ``None``, ``float('nan')``, ``pandas.NA`` and
    ``pandas.NaT``. The last two are checked by type name so a plain
    ``str(value)`` cannot turn them into ``"<NA>"`` / ``"NaT"``.
    """
    if value is None:
        return ""
    if type(value).__name__ in _NA_TYPE_NAMES:
        return ""
    try:
        if value != value:  # NaN, and anything else unequal to itself
            return ""
    except Exception:  # noqa: BLE001 - some sentinels raise on comparison
        return ""
    return str(value).strip()


def _as_float(value: Any) -> Optional[float]:
    """``float(value)`` or ``None``. Never raises."""
    if value is None:
        return None
    if type(value).__name__ in _NA_TYPE_NAMES:
        return None
    try:
        if value != value:  # NaN
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_synonyms(value: Any) -> tuple[str, ...]:
    """Synonyms as a deterministic tuple.

    Accepts a list, a set, or a pipe-delimited string — the three shapes that
    exist in the wild, because ``extra`` was a pipe string before it was JSON. A
    set is sorted so two runs cannot disagree.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split("|") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(str(item).strip() for item in value if str(item).strip()))
    text_value = _clean_text(value)
    return (text_value,) if text_value else ()


def _parse_extra(value: Any) -> dict[str, Any]:
    """Parse a ``dim_concept.extra`` / ``dim_vocabulary.extra`` cell into a dict.

    Accepts, in order: a mapping, a JSON object, a JSON list (read as a synonym
    list), and the legacy pipe-delimited synonym string. Never raises — a
    malformed ``extra`` degrades to "no synonyms", because losing a synonym is
    a worse outcome than aborting a vocabulary load over one bad cell.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return {"synonyms": list(value)}

    text_value = _clean_text(value)
    if not text_value:
        return {}
    if text_value[0] in "{[":
        try:
            parsed = json.loads(text_value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"synonyms": parsed}
    return {"synonyms": text_value}


def _concept_rank(extra: Mapping[str, Any]) -> int:
    """``extra['rank']`` as an int, defaulting to 0.

    ``dim_concept`` has no rank column, so a vocabulary that wants to settle a
    duplicate name says so in ``extra``. Unparseable values are 0, which keeps
    the resolver's tie-break total: 0 is a value, not an absence.
    """
    raw = (extra or {}).get("rank")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


#: Columns lifted from the vocabulary CSV into ``dim_concept.extra``.
#:
#: This whitelist is the decision review C14 asked for: ``extra`` holds
#: structured evidence, not "whatever columns the builder happened to emit".
#: ``rank`` is included because it is the vocabulary's own tie-break between two
#: concepts sharing a name.
_EXTRA_PASSTHROUGH_COLUMNS: tuple[str, ...] = (
    "definition",
    "centroid_lat",
    "centroid_lon",
    "rank",
    "rank_weight",
)


def _extra_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the ``extra`` object for one CSV row.

    Enabled, in that case only when non-empty:
    ``synonyms`` (list), ``definition`` (str), ``centroid_lat`` / ``centroid_lon``
    (float), ``rank`` / ``rank_weight`` (int). Centroid columns are the ones
    ``build_location_vocabulary`` emits and the pre-refactor loader dropped
    (review C14); ``definition`` is what ``build_disease_vocabulary`` emits and
    the pre-refactor loader also dropped.
    """
    payload: dict[str, Any] = {}

    synonyms = _split_synonyms(row.get("synonyms"))
    if synonyms:
        payload["synonyms"] = list(synonyms)

    definition = _clean_text(row.get("definition"))
    if definition:
        payload["definition"] = definition

    for column in ("centroid_lat", "centroid_lon"):
        coordinate = _as_float(row.get(column))
        if coordinate is not None:
            payload[column] = coordinate

    for column in ("rank", "rank_weight"):
        raw = row.get(column)
        if raw is None:
            continue
        try:
            payload[column] = int(float(raw))
        except (TypeError, ValueError):
            continue

    return payload


def _sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, streamed, lower-case hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# =============================================================================
# VocabularyLoader
# =============================================================================

#: Attributes a pack must expose for :meth:`VocabularyLoader.load_csv`.
_REQUIRED_PACK_ATTRIBUTES: tuple[str, ...] = (
    "vocabulary_id",
    "pack_id",
    "version",
    "domain",
    "normalizer_chain",
)


class VocabularyLoader:
    """Loads one vocabulary release into ``dim_vocabulary`` + ``dim_concept``.

    One instance per session. It holds a small read cache for
    :meth:`_get`; :meth:`load_csv` invalidates the entries it touches.

    This class does **not** match anything. ``ConceptResolver`` does, and it
    reads ``dim_concept`` directly with its own indexed, ordered query.
    """

    def __init__(self, session=None) -> None:
        """Bind to a session.

        Parameters
        ----------
        session : sqlalchemy.orm.Session, optional
            The session to write through. ``None`` is accepted so the class can
            be constructed before a session exists; every method that touches
            the database raises ``RuntimeError`` until ``self.session`` is set.
        """
        self.session = session
        #: ``(vocabulary_id, domain) -> DataFrame``. Only `_get` reads it.
        self._cache: dict[tuple[str, Optional[str]], pd.DataFrame] = {}

    # -------------------------------------------------------------------------
    # Guards
    # -------------------------------------------------------------------------

    @staticmethod
    def _require_session(session) -> Any:
        """Return a usable session, or explain that none was bound."""
        if session is None:
            raise RuntimeError(
                "VocabularyLoader has no session. Construct it with "
                "VocabularyLoader(session) or assign `.session` before calling "
                "a database method."
            )
        return session

    @staticmethod
    def _require_pack(pack: Any, path: Any) -> Any:
        """Validate that ``pack`` looks like a ``ConceptPack``.

        Raises
        ------
        TypeError
            When ``pack`` is a string or a path — the shape of the *legacy*
            ``load_csv(vocab_id, path, domain)`` signature. Catching it here
            turns a silently mis-bound call into a message that names the new
            signature, which is the difference between a five-minute fix and an
            afternoon of debugging an empty vocabulary.
        """
        if isinstance(pack, (str, Path)):
            raise TypeError(
                f"load_csv now takes (path, pack), not a bare vocabulary id. "
                f"Received pack={pack!r} (a string) for path={path!r}. Build a "
                f"ConceptPack — registry.get(<pack_id>) — and pass it: "
                f"load_csv(csv_path, pack). Callers still using the old shape "
                f"are migrated in batch 6D."
            )

        missing = [name for name in _REQUIRED_PACK_ATTRIBUTES if not hasattr(pack, name)]
        if missing:
            raise TypeError(
                f"load_csv needs a ConceptPack-like object exposing "
                f"{', '.join(_REQUIRED_PACK_ATTRIBUTES)}; {type(pack).__name__} "
                f"is missing {', '.join(missing)}."
            )
        return pack

    @staticmethod
    def _pack_normalizer(pack: Any) -> normalizers.Normalizer:
        """The composed normaliser for the pack's declared chain.

        Resolved through ``NORMALIZERS`` by id — never by re-implementing a
        regex, and never by guessing a chain from a domain (review C8). An
        unknown id raises here, deliberately: a vocabulary whose
        ``concept_norm`` cannot be computed must not be loaded, because a wrong
        normalised key silently widens the matcher's L2 rung for as long as the
        row lives.
        """
        chain = tuple(getattr(pack, "normalizer_chain", ()) or ())
        return normalizers.compose(chain)

    # -------------------------------------------------------------------------
    # Reading
    # -------------------------------------------------------------------------

    def _get(self, vocabulary_id: str, domain: Optional[str] = None) -> pd.DataFrame:
        """Concept rows for one vocabulary release, in a fixed order.

        ``ORDER BY concept_code`` is the contract's determinism rule. Without
        it, two databases holding the same concepts in a different physical
        order can hand back a different row for the same raw value — review C9,
        which is invisible until two environments are diffed.

        ``dim_concept.extra`` is returned raw *and* parsed into the columns a
        caller actually wants (``synonyms`` as a pipe string, ``concept_rank``
        as an int). Reading it twice would eventually produce two readers that
        disagree about a legacy row.

        Returns
        -------
        pandas.DataFrame
            Columns: ``concept_id``, ``concept_code``, ``concept_name``,
            ``concept_class``, ``domain``, ``parent_code``, ``concept_norm``,
            ``extra`` (raw), ``synonyms`` (pipe-joined), ``concept_rank``.
            Empty (with the same columns) when the vocabulary holds nothing.

        Notes
        -----
        Read-only, and nothing in the resolution path calls it:
        ``ConceptResolver`` loads its own indexes with the same ordering. This
        exists for tooling, for the review queue and for a human checking what a
        release actually contains.
        """
        session = self._require_session(self.session)
        identifier = _clean_text(vocabulary_id)
        if not identifier:
            raise ValueError("_get needs a vocabulary_id")

        key = (identifier, domain)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        where = "vocabulary_id = :v"
        params: dict[str, Any] = {"v": identifier}
        if domain:
            where += " AND domain = :d"
            params["d"] = domain

        rows = session.execute(
            text(
                "SELECT concept_id, concept_code, concept_name, concept_class, "
                "domain, parent_code, concept_norm, extra "
                f"FROM dim_concept WHERE {where} "
                "ORDER BY concept_code"
            ),
            params,
        ).fetchall()

        columns = [
            "concept_id", "concept_code", "concept_name", "concept_class",
            "domain", "parent_code", "concept_norm", "extra",
        ]
        frame = pd.DataFrame(rows, columns=columns)

        if frame.empty:
            frame = frame.assign(
                synonyms=pd.Series(dtype="object"),
                concept_rank=pd.Series(dtype="int64"),
            )
        else:
            parsed = [_parse_extra(value) for value in frame["extra"]]
            frame = frame.assign(
                synonyms=[
                    "|".join(_split_synonyms(extra.get("synonyms")))
                    for extra in parsed
                ],
                concept_rank=[_concept_rank(extra) for extra in parsed],
            )

        self._cache[key] = frame
        return frame

    # -------------------------------------------------------------------------
    # Writing
    # -------------------------------------------------------------------------

    def load_csv(self, path: str | Path, pack: Any, *,
                 domain: Optional[str] = None,
                 checksum: Optional[str] = None,
                 dry_run: bool = False) -> int:
        """Load one vocabulary CSV under a pack's declared release.

        Parameters
        ----------
        path : str or pathlib.Path
            The vocabulary CSV. Must carry ``concept_code`` and
            ``concept_name``; everything else is optional.
        pack : ConceptPack
            The pack that governs this release. It is the **only** source of
            ``vocabulary_id``, ``version``, ``pack_id``, ``released_with``,
            ``domain`` and the normaliser chain — a caller cannot override any
            of them, because a vocabulary loaded under an id the resolver does
            not query is invisible, and that invisibility is review C10.
        domain : str, optional
            Override the pack's domain. Only for a release that legitimately
            spans two domains (a single CSV holding both diseases and vectors);
            the default is ``pack.domain`` and overriding it is logged.
        checksum : str, optional
            Pre-computed SHA-256 of the CSV. Defaults to hashing ``path``.
        dry_run : bool
            Parse and validate, report what would be written, write nothing.

        Returns
        -------
        int
            Number of concept rows written (0 on ``dry_run``).

        What is written
        ---------------
        ``dim_vocabulary``
            One upserted row: ``vocabulary_id`` (version-qualified),
            ``version`` (never NULL — review C5), ``pack_id``,
            ``released_with``, ``checksum`` (+ ``{"checksum_artefact": "csv"}``
            in ``extra`` where that column exists).
        ``dim_concept``
            One upserted row per concept: ``concept_class``, ``domain``,
            ``parent_code``, ``extra`` (JSON with synonyms / definition /
            centroid / rank — review C14), and ``concept_norm`` computed with
            the pack's chain so the resolver's L2 rung and the stored key cannot
            disagree.

        Raises
        ------
        TypeError
            If ``pack`` is not pack-shaped (including the legacy
            ``load_csv(vocab_id, path, domain)`` call order).
        ValueError
            If the pack declares no version, if the CSV lacks a required
            column, or if ``vocabulary_id`` is not version-qualified.
        FileNotFoundError
            If ``path`` does not exist.
        """
        session = self._require_session(self.session)
        pack = self._require_pack(pack, path)

        csv_path = Path(path)
        if not csv_path.is_file():
            raise FileNotFoundError(f"vocabulary CSV not found: {csv_path}")

        vocabulary_id = _clean_text(getattr(pack, "vocabulary_id", ""))
        pack_id = _clean_text(getattr(pack, "pack_id", ""))
        version = _clean_text(getattr(pack, "version", ""))
        released_with = _clean_text(getattr(pack, "released_with", "")) or None
        effective_domain = _clean_text(domain) or _clean_text(getattr(pack, "domain", ""))

        if not vocabulary_id:
            raise ValueError(
                f"pack {pack_id or '?'!r} exposes no vocabulary_id; a concept "
                f"cannot be written without the id the resolver queries."
            )
        if not version:
            raise ValueError(
                f"pack {pack_id or vocabulary_id!r} declares no version. "
                f"dim_vocabulary.version must never be written as NULL (review "
                f"C5): an unversioned vocabulary cannot be attributed to a "
                f"release, so 'which knowledge produced this decision' has no "
                f"answer."
            )
        if "@" not in vocabulary_id:
            raise ValueError(
                f"vocabulary_id {vocabulary_id!r} is not version-qualified. The "
                f"contract requires the 'pack@version' form ('gadm@4.1') so two "
                f"releases coexist; ConceptResolver filters on it literally, so "
                f"an unqualified id resolves nothing."
            )
        if not effective_domain:
            raise ValueError(
                f"pack {pack_id or vocabulary_id!r} declares no domain and none "
                f"was given; the resolver filters dim_concept by domain."
            )

        frame = pd.read_csv(csv_path)

        missing = [
            column
            for column in ("concept_code", "concept_name")
            if column not in frame.columns
        ]
        if missing:
            raise ValueError(
                f"{csv_path}: vocabulary CSV is missing required column(s) "
                f"{', '.join(missing)}; a concept needs a code and a name."
            )

        normalise = self._pack_normalizer(pack)
        digest = checksum or _sha256_file(csv_path)

        records: list[dict[str, Any]] = []
        blank_code = 0
        blank_name = 0
        normaliser_failures = 0

        for row in frame.to_dict("records"):
            code = _clean_text(row.get("concept_code"))
            name = _clean_text(row.get("concept_name"))
            if not code:
                blank_code += 1
                continue
            if not name:
                blank_name += 1
                continue

            try:
                concept_norm = normalise(name) or None
            except Exception as exc:  # noqa: BLE001 - one bad cell must not stop a load
                normaliser_failures += 1
                logger.warning(
                    "%s: the pack's normaliser raised %s for concept %r (%r); "
                    "storing a NULL concept_norm so the L2 rung simply skips "
                    "this concept.",
                    csv_path.name, type(exc).__name__, code, name,
                )
                concept_norm = None

            records.append({
                "v": vocabulary_id,
                "c": code,
                "n": name,
                "k": _clean_text(row.get("concept_class")) or None,
                "d": effective_domain,
                "p": _clean_text(row.get("parent_code")) or None,
                "e": extra_json(_extra_payload(row)),
                "norm": concept_norm,
            })

        if blank_code or blank_name:
            logger.warning(
                "%s: skipped %d row(s) with a blank concept_code and %d with a "
                "blank concept_name; neither can be written to dim_concept.",
                csv_path, blank_code, blank_name,
            )

        if dry_run:
            logger.info(
                "[dry run] %s: would write %d concept(s) as %r (version %r, "
                "pack %r, domain %r, checksum %s…)",
                csv_path.name, len(records), vocabulary_id, version, pack_id,
                effective_domain, digest[:12],
            )
            return 0

        supports_extra = dim_vocabulary_supports_extra(session)
        artefact = checksum_artefact_json(CHECKSUM_ARTEFACT_CSV)

        vocabulary_columns = [
            "vocabulary_id", "vocabulary_name", "source_url", "version",
            "description", "pack_id", "checksum", "released_with",
        ]
        vocabulary_values = [
            ":vocabulary_id", ":vocabulary_name", ":source_url", ":version",
            ":description", ":pack_id", ":checksum", ":released_with",
        ]
        vocabulary_params: dict[str, Any] = {
            "vocabulary_id": vocabulary_id,
            "vocabulary_name": vocabulary_id,
            "source_url": None,
            "version": version,
            "description": f"fields: {effective_domain} (csv {csv_path.name})",
            "pack_id": pack_id or None,
            "checksum": digest,
            "released_with": released_with,
        }
        update_assignments = [
            "vocabulary_name = EXCLUDED.vocabulary_name",
            "version         = EXCLUDED.version",
            "description     = EXCLUDED.description",
            "pack_id         = EXCLUDED.pack_id",
            "checksum        = EXCLUDED.checksum",
            "released_with   = EXCLUDED.released_with",
        ]
        if supports_extra:
            vocabulary_columns.append("extra")
            vocabulary_values.append(":extra")
            vocabulary_params["extra"] = artefact
            update_assignments.append("extra           = EXCLUDED.extra")

        session.execute(
            text(
                f"INSERT INTO dim_vocabulary ({', '.join(vocabulary_columns)}) "
                f"VALUES ({', '.join(vocabulary_values)}) "
                f"ON CONFLICT (vocabulary_id) DO UPDATE SET "
                + ", ".join(update_assignments)
            ),
            vocabulary_params,
        )

        if records:
            session.execute(
                text(
                    "INSERT INTO dim_concept "
                    "(vocabulary_id, concept_code, concept_name, concept_class, "
                    " domain, parent_code, extra, concept_norm) "
                    "VALUES (:v, :c, :n, :k, :d, :p, :e, :norm) "
                    "ON CONFLICT (vocabulary_id, concept_code) DO UPDATE SET "
                    "concept_name  = EXCLUDED.concept_name, "
                    "concept_class = EXCLUDED.concept_class, "
                    "domain        = EXCLUDED.domain, "
                    "parent_code   = EXCLUDED.parent_code, "
                    "extra         = EXCLUDED.extra, "
                    "concept_norm  = EXCLUDED.concept_norm"
                ),
                records,
            )

        session.commit()

        # Invalidate rather than refresh: `_get` parses `extra` into extra
        # columns, and a stale entry would be handed to a caller as if it had
        # came from the database.
        for key in [key for key in self._cache if key[0] == vocabulary_id]:
            self._cache.pop(key, None)

        logger.info(
            "loaded %d concept(s) as %r (version %r, pack %r, domain %r, "
            "checksum %s…, artefact %r, checksum_artefact_in_extra=%s%s)",
            len(records), vocabulary_id, version, pack_id or "?", effective_domain,
            digest[:12], CHECKSUM_ARTEFACT_CSV, supports_extra,
            f", {normaliser_failures} NULL concept_norm" if normaliser_failures else "",
        )
        return len(records)


__all__ = [
    "CHECKSUM_ARTEFACT_CSV",
    "CHECKSUM_ARTEFACT_MANIFEST",
    "VocabularyLoader",
    "checksum_artefact_json",
    "dim_vocabulary_supports_extra",
    "extra_json",
]