# -*- coding: utf-8 -*-
"""
Vocabulary ingestion: one vocabulary release, loaded once, verifiably.
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


CHECKSUM_ARTEFACT_MANIFEST = "manifest"

CHECKSUM_ARTEFACT_CSV = "csv"


def extra_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON text for a ``dim_*`` ``extra`` column.
    """
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))


def checksum_artefact_json(artefact: str) -> str:
    """``{"checksum_artefact": <artefact>}`` as canonical JSON text."""
    return extra_json({"checksum_artefact": str(artefact)})


_EXTRA_SUPPORT_CACHE: dict[str, bool] = {}
_EXTRA_SUPPORT_WARNED: set[str] = set()


def dim_vocabulary_supports_extra(session) -> bool:
    """Whether ``dim_vocabulary`` carries the ``extra`` JSON column.
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


_NA_TYPE_NAMES = frozenset({"NAType", "NaTType"})


def _clean_text(value: Any) -> str:
    """``str(value).strip()`` with ``None``/NaN/``pd.NA`` mapped to ``''``.
    """
    if value is None:
        return ""
    if type(value).__name__ in _NA_TYPE_NAMES:
        return ""
    try:
        if value != value: 
            return ""
    except Exception:  
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
    raw = (extra or {}).get("rank")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


_EXTRA_PASSTHROUGH_COLUMNS: tuple[str, ...] = (
    "definition",
    "centroid_lat",
    "centroid_lon",
    "rank",
    "rank_weight",
)


def _extra_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the ``extra`` object for one CSV row.
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

_REQUIRED_PACK_ATTRIBUTES: tuple[str, ...] = (
    "vocabulary_id",
    "pack_id",
    "version",
    "domain",
    "normalizer_chain",
)


class VocabularyLoader:
    """Loads one vocabulary release into ``dim_vocabulary`` + ``dim_concept``.
    """

    def __init__(self, session=None) -> None:
        """Bind to a session.
        """
        self.session = session
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
        """
        chain = tuple(getattr(pack, "normalizer_chain", ()) or ())
        return normalizers.compose(chain)

    # -------------------------------------------------------------------------
    # Reading
    # -------------------------------------------------------------------------

    def _get(self, vocabulary_id: str, domain: Optional[str] = None) -> pd.DataFrame:
        """Concept rows for one vocabulary release, in a fixed order.
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