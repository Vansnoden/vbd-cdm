"""
Extract disease and malaria terms from IDOMAL (OBO format).

IDOMAL is the malaria ontology, built on BFO and following OBO Foundry rules.
Download: http://purl.obolibrary.org/obo/idomal.obo
"""

import pronto
import pandas as pd
from typing import Optional


def build_disease_vocabulary(obo_path: str, output_csv: str,
                             root_terms: Optional[list] = None) -> pd.DataFrame:
    """
    Extract terms from an OBO ontology.

    Args:
        obo_path: Path to the .obo file (IDOMAL, IDO, MONDO, ...)
        output_csv: Where to write the vocabulary
        root_terms: If provided, only keep descendants of these term IDs.
                    For IDOMAL, e.g. ['IDOMAL:0000000'] for the whole ontology.
    """
    ont = pronto.Ontology(obo_path)

    rows = []
    for term in ont.terms():
        if term.obsolete:
            continue
        if root_terms and not any(term.id in r or r in term.superclasses(distance=100)
                                  for r in root_terms):
            continue

        rows.append({
            "concept_code":  term.id,                     # 'IDOMAL:0000657'
            "concept_name":  term.name or "",             # preferred label
            "concept_class": _infer_class(term),          # 'disease', 'vector', 'intervention'
            "parent_code":   next((p.id for p in term.superclasses(distance=1)), None),
            "synonyms":      "|".join(s.description for s in term.synonyms),
            "definition":    (term.definition or "")[:500],
        })

    df = pd.DataFrame(rows)
    df["vocabulary_id"] = _detect_vocab_id(obo_path)   # 'IDOMAL', 'IDO', 'MONDO'
    df["domain"] = "disease"
    df["name_normalized"] = df["concept_name"].str.lower().str.strip()

    df.to_csv(output_csv, index=False)
    return df


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


def _detect_vocab_id(path: str) -> str:
    name = path.lower()
    if "idomal" in name: return "IDOMAL"
    if "mondo" in name:  return "MONDO"
    if "ido"   in name:  return "IDO"
    if "miro"  in name:  return "MIRO"
    return "UNKNOWN"
