"""
Loads vocabulary CSVs into dim_vocabulary / dim_concept and provides
matching utilities for standardizing raw CDM values.
"""

import pandas as pd
from rapidfuzz import fuzz, process
from sqlalchemy import text
from typing import Optional, Tuple


class VocabularyLoader:
    def __init__(self, session):
        self.session = session
        self._cache = {}          # (vocab_id, domain) → DataFrame

    def load_csv(self, vocab_id: str, path: str, domain: str):
        """Insert a vocabulary CSV into dim_vocabulary + dim_concept."""
        df = pd.read_csv(path)
        df["vocabulary_id"] = vocab_id
        df["domain"] = domain
        df["name_normalized"] = df["concept_name"].str.lower().str.strip()

        # Upsert the vocabulary row
        self.session.execute(text("""
            INSERT INTO dim_vocabulary (vocabulary_id, vocabulary_name, source_url, version)
            VALUES (:id, :name, :url, :ver)
            ON CONFLICT (vocabulary_id) DO UPDATE SET
                vocabulary_name = EXCLUDED.vocabulary_name,
                version         = EXCLUDED.version
        """), {"id": vocab_id, "name": vocab_id, "url": None, "ver": None})

        # Upsert concepts
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

    def match(self, raw_value: str, vocab_id: str, domain: str,
              threshold: float = 0.85
              ) -> Tuple[Optional[dict], str, float]:
        """
        Match a raw value against a vocabulary.

        Returns (concept_dict_or_None, method, score).
        method ∈ {'exact', 'normalized', 'fuzzy', 'none'}.
        """
        if not raw_value:
            return None, "none", 0.0

        df = self._get(vocab_id, domain)
        if df.empty:
            return None, "none", 0.0

        # 1. Exact match
        norm = raw_value.lower().strip()
        exact = df[df["concept_name"].str.lower().str.strip() == norm]
        if not exact.empty:
            return exact.iloc[0].to_dict(), "exact", 1.0

        # 2. Normalized match (strip diacritics, collapse whitespace)
        import unicodedata
        def norm_deep(s):
            s = unicodedata.normalize("NFKD", str(s))
            s = "".join(c for c in s if not unicodedata.combining(c))
            return " ".join(s.lower().split())
        target = norm_deep(raw_value)
        normalized = df[df["concept_name"].apply(norm_deep) == target]
        if not normalized.empty:
            return normalized.iloc[0].to_dict(), "normalized", 0.95

        # 3. Fuzzy match (token set ratio — good for word reorderings)
        choices = df["concept_name"].tolist()
        best = process.extractOne(raw_value, choices, scorer=fuzz.token_set_ratio)
        if best and best[1] >= threshold * 100:
            concept = df[df["concept_name"] == best[0]].iloc[0].to_dict()
            return concept, "fuzzy", best[1] / 100.0

        return None, "none", 0.0



def match_value(session, value, vocab_id, domain, threshold=0.85):
    return VocabularyLoader(session).match(value, vocab_id, domain, threshold)