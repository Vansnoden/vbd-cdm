"""
CLI to build all vocabulary CSVs.

Usage:
    python -m utilities.vocabulary.build --gadm path/to/gadm41_KEN.gpkg
                                          --idomal path/to/idomal.obo
                                          --out vocabularies/
"""

import argparse
from pathlib import Path

from utilities.vocabulary.locations import build_location_vocabulary
from utilities.vocabulary.diseases  import build_disease_vocabulary
from utilities.vocabulary.species   import build_species_vocabulary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gadm",    required=True)
    ap.add_argument("--idomal",  required=False)
    ap.add_argument("--out",     default="vocabularies")
    args = ap.parse_args()

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)

    print("Building location vocabulary from GADM …")
    build_location_vocabulary(args.gadm, out / "gadm_locations.csv")

    if args.idomal:
        print("Building disease vocabulary from IDOMAL …")
        build_disease_vocabulary(args.idomal, out / "idomal_diseases.csv")

    print("Building species vocabulary from NCBI …")
    build_species_vocabulary(out / "ncbi_vectors.csv")

    print(f"Done. Vocabulary CSVs in {out}/")


if __name__ == "__main__":
    main()
