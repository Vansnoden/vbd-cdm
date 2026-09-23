"""
Loads vocabulary CSVs into dim_vocabulary / dim_concept and provides
matching utilities for standardizing raw CDM values.
"""

import re
import unicodedata
from typing import Dict, Optional, Tuple

import pandas as pd
from rapidfuzz import fuzz, process
from sqlalchemy import text


# =============================================================================
# Normalisation helpers
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
# VocabularyLoader
# =============================================================================

class VocabularyLoader:
    def __init__(self, session):
        self.session = session
        self._cache: Dict[Tuple[str, str], pd.DataFrame] = {}
        # (vocab_id, 'vector') -> {normalised_key: concept_row_dict}
        self._species_index: Dict[Tuple[str, str], Dict[str, dict]] = {}

    # -------------------------------------------------------------------------
    # Loading
    # -------------------------------------------------------------------------

    def load_csv(self, vocab_id: str, path: str, domain: str):
        """Insert a vocabulary CSV into dim_vocabulary + dim_concept."""
        df = pd.read_csv(path)
        df["vocabulary_id"] = vocab_id
        df["domain"] = domain
        df["name_normalized"] = df["concept_name"].str.lower().str.strip()

        # Upsert the vocabulary row.
        self.session.execute(text("""
            INSERT INTO dim_vocabulary (vocabulary_id, vocabulary_name, source_url, version)
            VALUES (:id, :name, :url, :ver)
            ON CONFLICT (vocabulary_id) DO UPDATE SET
                vocabulary_name = EXCLUDED.vocabulary_name,
                version         = EXCLUDED.version
        """), {"id": vocab_id, "name": vocab_id, "url": None, "ver": None})

        # Upsert concepts.
        for _, row in df.iterrows():
            self.session.execute(text("""
                INSERT INTO dim_concept
                    (vocabulary_id, concept_code, concept_name,
                     concept_class, domain, parent_code, extra)
                VALUES (:v, :c, :n, :k, :d, :p, :e)
                ON CONFLICT (vocabulary_id, concept_code) DO UPDATE SET
                    concept_name  = EXCLUDED.concept_name,
                    concept_class = EXCLUDED.concept_class,
                    parent_code   = EXCLUDED.parent_code,
                    extra         = EXCLUDED.extra
            """), {
                "v": vocab_id, "c": str(row["concept_code"]),
                "n": row["concept_name"], "k": row.get("concept_class"),
                "d": domain, "p": row.get("parent_code"),
                "e": row.get("synonyms"),
            })

        self.session.commit()
        self._cache[(vocab_id, domain)] = df
        # Drop any species index built from a previous version of this CSV
        # so it will be rebuilt on next use.
        self._species_index.pop((vocab_id, "vector"), None)

    # -------------------------------------------------------------------------
    # Caches
    # -------------------------------------------------------------------------

    def _get(self, vocab_id: str, domain: str) -> pd.DataFrame:
        key = (vocab_id, domain)
        if key not in self._cache:
            rows = self.session.execute(text("""
                SELECT concept_code, concept_name, concept_class,
                       parent_code, extra AS synonyms
                FROM dim_concept
                WHERE vocabulary_id = :v AND domain = :d
            """), {"v": vocab_id, "d": domain}).fetchall()
            self._cache[key] = pd.DataFrame(rows, columns=[
                "concept_code", "concept_name", "concept_class",
                "parent_code", "synonyms"
            ])
        return self._cache[key]

    def _get_species_index(self, vocab_id: str) -> Dict[str, dict]:
        """
        Lazily build the normalised-key -> concept-row map for the
        vector domain of one vocabulary.

        Both the concept_name and every pipe-separated entry in the
        synonyms column are indexed under _norm_species. First writer
        wins on collision; the vocabulary CSV is assumed to be curated
        so collisions should be rare.
        """
        key = (vocab_id, "vector")
        if key in self._species_index:
            return self._species_index[key]

        df = self._get(vocab_id, "vector")
        idx: Dict[str, dict] = {}
        if not df.empty:
            for _, row in df.iterrows():
                # Index the concept name itself.
                k = _norm_species(row["concept_name"])
                if k and k not in idx:
                    idx[k] = row.to_dict()

                # Index each synonym.
                syns = row.get("synonyms") or ""
                if isinstance(syns, str) and syns:
                    for syn in syns.split("|"):
                        sk = _norm_species(syn)
                        if sk and sk not in idx:
                            idx[sk] = row.to_dict()

        self._species_index[key] = idx
        return idx

    # -------------------------------------------------------------------------
    # Matching
    # -------------------------------------------------------------------------

    def match(self, raw_value: str, vocab_id: str, domain: str,
              threshold: float = 0.85
              ) -> Tuple[Optional[dict], str, float]:
        """
        Match a raw value against a vocabulary.

        Returns (concept_dict_or_None, method, score).

        method is one of:
            'exact'       - case-insensitive exact match on concept_name
            'normalized'  - matched after domain-specific normalisation
                            (species normalisation for the vector domain,
                            diacritic stripping for everything else)
            'fuzzy'       - token_set_ratio above `threshold`
            'none'        - no match
        """
        if not raw_value:
            return None, "none", 0.0

        df = self._get(vocab_id, domain)
        if df.empty:
            return None, "none", 0.0

        # ---- 1. Exact match -------------------------------------------------
        norm = str(raw_value).lower().strip()
        exact = df[df["concept_name"].str.lower().str.strip() == norm]
        if not exact.empty:
            return exact.iloc[0].to_dict(), "exact", 1.0

        # ---- 2a. Species-normalised match (vector domain only) --------------
        if domain == "vector":
            species_key = _norm_species(raw_value)
            if species_key:
                idx = self._get_species_index(vocab_id)
                hit = idx.get(species_key)
                if hit is not None:
                    return hit, "normalized", 0.95

        # ---- 2b. Generic normalised match -----------------------------------
        target = _norm_deep(raw_value)
        normalized = df[df["concept_name"].apply(_norm_deep) == target]
        if not normalized.empty:
            return normalized.iloc[0].to_dict(), "normalized", 0.95

        # ---- 3. Fuzzy match -------------------------------------------------
        choices = df["concept_name"].tolist()
        best = process.extractOne(raw_value, choices, scorer=fuzz.token_set_ratio)
        if best and best[1] >= threshold * 100:
            concept = df[df["concept_name"] == best[0]].iloc[0].to_dict()
            return concept, "fuzzy", best[1] / 100.0

        return None, "none", 0.0


# =============================================================================
# Convenience wrapper
# =============================================================================

def match_value(session, value, vocab_id, domain, threshold=0.85):
    return VocabularyLoader(session).match(value, vocab_id, domain, threshold)