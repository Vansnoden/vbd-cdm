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


def main(argv: Optional[List[str]] = None) -> int:
    """Build a vocabulary CSV. Returns a process exit status.

    Subcommands
    -----------
    ``diseases``
        Build from an OBO/OWL ontology. Requires ``--obo-path``, ``--output``
        and ``--vocabulary-id``.

    Exit status
    -----------
    0   the CSV was written (and loaded, with ``--load``)
    1   the build failed; the message is on stderr
    2   the build succeeded but the optional CDM load could not run (no
        registry, or no pack governs the disease domain)
    """
    parser = argparse.ArgumentParser(
        prog="python -m utilities.vocabulary.build",
        description=(
            "Build the vocabulary CSVs the registry loads. Every builder states "
            "the vocabulary identity explicitly: identity is never inferred from "
            "a filename (review C11)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    diseases = sub.add_parser(
        "diseases", help="build a disease vocabulary from an OBO/OWL ontology"
    )
    diseases.add_argument(
        "--obo-path", required=True, type=Path,
        help="the ontology file to parse (e.g. idomal.obo, mondo.obo)",
    )
    diseases.add_argument(
        "--output", required=True, type=Path,
        help="where to write the vocabulary CSV",
    )
    diseases.add_argument(
        "--vocabulary-id", required=True,
        help=(
            "the identity every concept is stamped with, e.g. IDOMAL or MONDO. "
            "This is the value the resolver queries; it is NOT derived from the "
            "filename."
        ),
    )
    diseases.add_argument(
        "--ontology-prefix", default=None,
        help="keep only concepts whose code starts with this prefix (e.g. 'IDOMAL:')",
    )
    diseases.add_argument(
        "--include-obsolete", action="store_true",
        help="keep terms flagged obsolete in the source",
    )
    diseases.add_argument(
        "--load", action="store_true",
        help="also load the CSV into dim_vocabulary/dim_concept via VocabularyLoader",
    )
    diseases.add_argument(
        "--pack-id", default=None,
        help=(
            "registry pack to load under. Default: the single pack whose domain "
            "is 'disease'. Required only when more than one pack declares it."
        ),
    )
    diseases.add_argument(
        "--log-level", default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity (default: INFO)",
    )

    locations = sub.add_parser(
        "locations",
        help="build the boundary vocabulary (delegates; unchanged by batch 6D)",
    )
    locations.add_argument("remainder", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level if hasattr(args, "log_level") else "INFO"),
        stream=sys.stdout,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.command == "locations":
        # The boundary builder keeps its own entry point: it needs geopandas and
        # a manifest-declared layer layout, neither of which this batch touches.
        logger.error(
            "the 'locations' builder is not re-wired by batch 6D; call "
            "build_location_vocabulary directly with the boundary pack."
        )
        return 1

    try:
        frame = build_disease_vocabulary(
            args.obo_path,
            args.output,
            vocabulary_id=args.vocabulary_id,
            ontology_prefix=args.ontology_prefix,
            include_obsolete=bool(args.include_obsolete),
        )
    except Exception as exc:  # noqa: BLE001 - a CLI reports, it does not trace
        logger.error(
            "disease vocabulary build failed: %s: %s", type(exc).__name__, exc,
            exc_info=True,
        )
        return 1

    logger.info(
        "wrote %d concept(s) to %s under vocabulary_id %r",
        len(frame), args.output, str(args.vocabulary_id).strip(),
    )

    if not args.load:
        return 0

    # ---- optional load into the CDM ------------------------------------
    # Imported lazily: building a CSV must not require a database, and
    # `database.session` binds an engine at import time.
    try:
        from database.session import SessionLocal
        from utilities.vocabulary.loader import VocabularyLoader
        from utilities.vocabulary.registry import VocabularyRegistry
    except Exception as exc:  # noqa: BLE001
        logger.error("cannot load the CSV: %s: %s", type(exc).__name__, exc)
        return 2

    registry_path = Path(__file__).resolve().parents[2] / "vocabularies" / "registry"
    if not registry_path.is_dir():
        logger.error(
            "cannot load the CSV: no registry at %s. Build the packs first, or "
            "omit --load.", registry_path,
        )
        return 2

    registry = VocabularyRegistry.load_from(registry_path)

    if args.pack_id:
        try:
            pack = registry.get(args.pack_id)
        except Exception as exc:  # noqa: BLE001 - UnknownPack
            logger.error("cannot load the CSV: %s", exc)
            return 2
    else:
        candidates = [pack for pack in registry.packs() if pack.domain == "disease"]
        if len(candidates) != 1:
            logger.error(
                "cannot choose a pack to load under: %d pack(s) declare domain "
                "'disease' (%s). Pass --pack-id.",
                len(candidates),
                ", ".join(pack.pack_id for pack in candidates) or "none",
            )
            return 2
        pack = candidates[0]

    declared_identity = str(args.vocabulary_id).strip()
    if declared_identity.upper() != str(pack.pack_id).upper():
        logger.warning(
            "the CSV is stamped %r but pack %r is %r. Loading it will record the "
            "concepts under the pack's version-qualified id (%r), so the "
            "concepts you just built will NOT be visible to the resolver unless "
            "the two agree. Pass the identity the manifest declares.",
            declared_identity, pack.pack_id, pack.pack_id, pack.vocabulary_id,
        )

    session = SessionLocal()
    try:
        written = VocabularyLoader(session).load_csv(args.output, pack)
    except Exception as exc:  # noqa: BLE001
        logger.error("loading the CSV failed: %s: %s", type(exc).__name__, exc)
        return 2
    finally:
        session.close()

    logger.info(
        "loaded %d concept(s) into dim_concept under %r", written, pack.vocabulary_id
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
