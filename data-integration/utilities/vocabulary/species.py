"""
Build a vector species vocabulary from NCBI Taxonomy.

Correct query strategy:
    1. Look up the genus taxid:      Anopheles[Scientific Name]
    2. Fetch every taxon in the      txid<taxid>[Subtree]
       subtree of that taxid
    3. Batch-efetch records in       Entrez.efetch(..., id=ids)
       chunks of 200

NCBI rate-limits unauthenticated requests to 3 req/s. This script sleeps
0.34 s between requests to stay under the limit. If you set an NCBI API key
via `Entrez.api_key`, you can raise this to 10 req/s (sleep 0.11).
"""

import time
import pandas as pd
from Bio import Entrez


# Curated list of vector genera relevant to VBD surveillance.
VECTOR_TAXA = [
    "Anopheles",
    "Aedes",
    "Culex",
    "Mansonia",
    "Ixodes",
    "Phlebotomus",
]

# NCBI rate limiting.
_REQUEST_INTERVAL = 0.34   # 3 req/s without API key
_FETCH_BATCH = 200         # efetch handles up to a few hundred IDs at once


def _sleep():
    time.sleep(_REQUEST_INTERVAL)


def _get_genus_taxid(genus: str) -> str | None:
    """
    Return the taxid of the *genus* named `genus`, ignoring subgenera,
    series, and other taxa that share the same name.

    NCBI's `[Scientific Name]` search returns every record whose name
    matches, and the esearch result does not include ranks — so we
    esearch for candidates, efetch them, and pick the one whose Rank is
    exactly 'genus'. This prevents the subtree walk from descending into
    a subgenus or a series node by mistake.
    """
    handle = Entrez.esearch(
        db="taxonomy",
        term=f"{genus}[Scientific Name]",
        retmax=20,
    )
    ids = Entrez.read(handle)["IdList"]
    handle.close()
    _sleep()

    if not ids:
        return None

    handle = Entrez.efetch(db="taxonomy", id=ids)
    try:
        records = Entrez.read(handle)
    finally:
        handle.close()
    _sleep()

    target = genus.strip().lower()

    # Preferred: exact name match whose rank is 'genus'.
    for rec in records:
        name = rec.get("ScientificName", "").strip().lower()
        rank = rec.get("Rank", "").strip().lower()
        if name == target and rank == "genus":
            return rec.get("TaxId")

    # Fallback: exact name match at any rank (should not normally fire).
    for rec in records:
        if rec.get("ScientificName", "").strip().lower() == target:
            return rec.get("TaxId")

    return None


def _get_subtree_ids(genus_taxid: str, cap: int = 20000) -> list[str]:
    """
    Return every taxid in the subtree rooted at `genus_taxid`.
    `cap` prevents runaway downloads for very large genera.
    """
    handle = Entrez.esearch(
        db="taxonomy",
        term=f"txid{genus_taxid}[Subtree]",
        retmax=cap,
    )
    ids = Entrez.read(handle)["IdList"]
    handle.close()
    _sleep()
    return ids


def _fetch_taxa(taxon_ids: list[str], genus: str) -> list[dict]:
    """Batch-fetch taxonomy records and normalize into rows."""
    rows = []
    for i in range(0, len(taxon_ids), _FETCH_BATCH):
        batch = taxon_ids[i:i + _FETCH_BATCH]
        handle = Entrez.efetch(db="taxonomy", id=batch)
        try:
            records = Entrez.read(handle)
        finally:
            handle.close()
        _sleep()

        for rec in records:
            # Scientific name + rank + parent
            rank = rec.get("Rank", "")
            name = rec.get("ScientificName", "")
            parent = rec.get("ParentTaxId", "")
            taxid = rec.get("TaxId", "")

            # Synonyms live in OtherNames → Synonym (a list of strings)
            other = rec.get("OtherNames", {})
            syns = other.get("Synonym", []) if isinstance(other, dict) else []
            if isinstance(syns, str):
                syns = [syns]

            rows.append({
                "concept_code":  taxid,
                "concept_name":  name,
                "concept_class": rank,
                "parent_code":   parent,
                "synonyms":      "|".join(syns),
                "genus":         genus,
            })
    return rows


def build_species_vocabulary(
    output_csv: str,
    genera: list | None = None,
    email: str = "you@example.com",
) -> pd.DataFrame:
    """
    Fetch species and subspecies under each vector genus from NCBI Taxonomy.

    Parameters
    ----------
    output_csv : str
        Where to write the resulting vocabulary CSV.
    genera : list[str] | None
        Genera to fetch. Defaults to VECTOR_TAXA.
    email : str
        Your email — required by NCBI's terms of use.

    Returns
    -------
    pd.DataFrame
        The vocabulary that was written.
    """
    Entrez.email = email
    genera = genera or VECTOR_TAXA

    all_rows = []
    for genus in genera:
        print(f"[{genus}] looking up taxid ...", flush=True)
        genus_taxid = _get_genus_taxid(genus)
        if not genus_taxid:
            print(f"[{genus}] no taxid found; skipping", flush=True)
            continue

        print(f"[{genus}] taxid={genus_taxid}; fetching subtree ...", flush=True)
        subtree_ids = _get_subtree_ids(genus_taxid)
        print(f"[{genus}] {len(subtree_ids)} taxa in subtree", flush=True)

        # Remove the genus record itself from the fetch so we only get
        # species/subspecies/etc.
        subtree_ids = [i for i in subtree_ids if i != genus_taxid]

        rows = _fetch_taxa(subtree_ids, genus)
        print(f"[{genus}] fetched {len(rows)} records", flush=True)
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    if df.empty:
        print("No species fetched. Check network / Entrez availability.")
        df.to_csv(output_csv, index=False)
        return df

    df["vocabulary_id"] = "NCBI"
    df["domain"] = "vector"
    df["name_normalized"] = df["concept_name"].astype(str).str.lower().str.strip()
    df.to_csv(output_csv, index=False)
    print(f"\nWrote {len(df)} records to {output_csv}")
    return df