# -*- coding: utf-8 -*-
"""CDM Integration Framework - orchestrator.

Reads ``base_config.yaml``, runs the ETL for every configured source, then the
consolidation step, then the summary.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    for _candidate in (PROJECT_ROOT / ".env", Path.cwd() / ".env"):
        if _candidate.is_file():
            load_dotenv(_candidate)
            break
    load_dotenv()
except Exception:  
    pass

from database import models                                    
from database.geoutils import get_iso_country_code              
from database.session import (                                  
    DATABASE_URL, SessionLocal, engine,
)
from etl.extractors.file_extractor import FileExtractor         
from etl.loaders.db_loader import DBLoader                      
from etl.transformers.mapping_transformer import MappingTransformer  
from utilities.consolidation import (                           
    DataConsolidator, enrich_location_dataframe, run_consolidation,
)
from utilities.data_transform_library import load_yaml_config    
from utilities.database_summary import (                         
    export_all_table_samples, schema_summary_to_csv,
)
from utilities.vocabulary.registry import VocabularyRegistry     

(PROJECT_ROOT / "logs").mkdir(exist_ok=True)
(PROJECT_ROOT / "outputs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(PROJECT_ROOT / "logs" / "etl.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "vocabularies" / "registry"
DIM_TIME_START = "1900-01-01"
DIM_TIME_END = "2026-12-31"


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------

class RunContext:
    """One run's identity: run id, registry, digest, countries.

    Threaded through ingestion, consolidation and the concept pass so every
    ``Resolution`` names the same run and the same vocabulary release.
    """

    def __init__(self, registry: Optional[VocabularyRegistry] = None,
                 run_id: Optional[str] = None,
                 countries: Optional[List[str]] = None) -> None:
        self.registry = registry if registry is not None else load_run_registry()
        self.run_id = run_id or datetime.utcnow().strftime("%Y%m%dT%H%M%S-") + \
            uuid.uuid4().hex[:6]
        self.countries = list(countries or [])
        self.registry_digest = (self.registry.digest()
                                if self.registry is not None else "")
        self.started_at = datetime.utcnow()

    def describe(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "registry_digest": self.registry_digest,
                "countries": list(self.countries),
                "packs": (len(self.registry.packs())
                          if self.registry is not None else 0)}


def load_run_registry(path: Optional[Path] = None) -> Optional[VocabularyRegistry]:
    """Load the shipped packs once. A missing directory is a warning, not a
    failure; a malformed manifest raises."""
    target = Path(path or DEFAULT_REGISTRY_PATH)
    if not target.is_dir():
        logger.warning(
            "no vocabulary registry at %s; vocabulary standardisation is skipped "
            "for this run. Build the packs, or point DEFAULT_REGISTRY_PATH at "
            "them.", target)
        return None
    registry = VocabularyRegistry.load_from(target)
    logger.info("registry %s: %d concept pack(s), digest %s",
                target, len(registry.packs()), registry.digest()[:12])
    return registry


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_environment() -> None:
    """Seed ``dim_time`` and make sure the output directories exist.

    ``load_dotenv()`` already ran at import time, before ``database.session``
    resolved its URL; calling it again here is a harmless no-op kept so a caller
    that imports this module for its helpers gets the same environment.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  
        pass
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    (PROJECT_ROOT / "outputs").mkdir(exist_ok=True)
    init_dim_time(DIM_TIME_START, DIM_TIME_END)


def init_dim_time(start_date: str, end_date: str) -> None:
    """Seed ``dim_time`` over a date range. Idempotent."""
    logger.info("Initializing dim_time from %s to %s", start_date, end_date)
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if start > end:
        raise ValueError(
            f"start_date ({start_date}) must be before end_date ({end_date})")

    with SessionLocal() as session:
        existing = {row[0] for row in session.execute(
            text("SELECT date_id FROM dim_time"))}
        current = start
        pending = []
        while current <= end:
            date_id = current.strftime("%Y-%m-%d")
            if date_id not in existing:
                pending.append(models.DimTime(
                    date_id=date_id, date=current.date(), year=current.year,
                    month=current.month, week=current.isocalendar()[1],
                    day_of_year=current.timetuple().tm_yday,
                    season=("dry" if current.month in (11, 12, 1, 2, 3)
                            else "wet"),
                    is_holiday=False))
            current += timedelta(days=1)

        if pending:
            session.bulk_save_objects(pending)
            session.commit()
        logger.info("  created %d new time record(s), %d already existed",
                    len(pending), len(existing))


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------

def get_all_files_from_folder(folder_path: str, extension: str) -> List[str]:
    ext = extension.lower()
    return [
        os.path.join(root, name)
        for root, _dirs, filenames in os.walk(folder_path)
        for name in filenames if name.lower().endswith(f".{ext}")
    ]


def get_file_paths(source_config: Dict[str, Any]) -> List[str]:
    extension = str(source_config.get("extension") or "").lower()
    source_path = source_config.get("path")
    if not extension:
        raise ValueError("Source missing 'extension' field")
    if not source_path:
        raise ValueError("Source missing 'path' field")
    if not os.path.isabs(source_path):
        source_path = str(PROJECT_ROOT / source_path)

    if source_config.get("type", "file") == "folder":
        paths = get_all_files_from_folder(source_path, extension)
        if paths:
            logger.info("Found %d .%s file(s) in %s",
                        len(paths), extension, source_path)
        else:
            logger.warning("No .%s files found in %s", extension, source_path)
        return paths
    return [source_path]


def get_mapping_path(source_config: Dict[str, Any]) -> Optional[str]:
    mapping_path = source_config.get("matching")
    if not mapping_path:
        return None
    if not os.path.isabs(mapping_path):
        mapping_path = str(PROJECT_ROOT / mapping_path)
    return mapping_path


# ---------------------------------------------------------------------------
# Transform / load
# ---------------------------------------------------------------------------

DIMENSION_TABLES = [
    "dim_source", "dim_vector", "dim_location", "dim_disease",
    "dim_insecticide", "dim_identification_method",
    "dim_resistance_test_type", "dim_socioeconomic_indicator",
]

FACT_TABLES = [
    "fact_vector_observation", "fact_disease_case",
    "fact_insecticide_resistance", "fact_bionomics", "fact_intervention",
    "fact_socioeconomic_condition", "fact_health_facility",
    "fact_disease_burden", "fact_environment",
]


def _transformer_for(source_config: Dict[str, Any], context: RunContext,
                     mapping_path: Optional[str]) -> MappingTransformer:
    """One transformer per pass, with the registry and run id attached.

    Per pass, not per source: a chunk's fact mappings must see the dimension rows
    that same chunk loaded, and the transformer's ``DimensionKeyIndex`` cache is
    seeded once per (table, field).
    """
    if mapping_path:
        return MappingTransformer(mapping_path, registry=context.registry,
                                  run_id=context.run_id)
    return MappingTransformer(column_specs=source_config.get("columns") or [],
                              registry=context.registry, run_id=context.run_id)


def transform_dimensions(df: pd.DataFrame, source_config: Dict[str, Any],
                         mapping_path: Optional[str],
                         context: RunContext) -> Dict[str, pd.DataFrame]:
    transformer = _transformer_for(source_config, context, mapping_path)
    try:
        built = transformer.transform(df, target_filter=set(DIMENSION_TABLES))
    finally:
        transformer.close()
    return {t: built[t] for t in DIMENSION_TABLES
            if t in built and not built[t].empty}


def transform_facts(df: pd.DataFrame, source_config: Dict[str, Any],
                    mapping_path: Optional[str],
                    context: RunContext) -> Dict[str, pd.DataFrame]:
    transformer = _transformer_for(source_config, context, mapping_path)
    try:
        built = transformer.transform(df, target_filter=set(FACT_TABLES))
    finally:
        transformer.close()
    return {t: built[t] for t in FACT_TABLES
            if t in built and not built[t].empty}


def load_dimensions(dim_dfs: Dict[str, pd.DataFrame], loader: DBLoader) -> int:
    """Load dimension tables in dependency order. Returns rows written."""
    total = 0
    for table_name in DIMENSION_TABLES:
        if table_name not in dim_dfs:
            continue
        frame = dim_dfs[table_name]
        if table_name == "dim_location":
            frame = enrich_location_dataframe(frame)
            if "location_id" in frame.columns:
                before = len(frame)
                frame = frame.dropna(subset=["location_id"])
                if len(frame) < before:
                    logger.warning("  removed %d row(s) with NULL location_id",
                                   before - len(frame))
        if frame.empty:
            continue
        total += loader.load_table(table_name, frame)
    return total


def load_facts(fact_dfs: Dict[str, pd.DataFrame], loader: DBLoader) -> int:
    """Load fact tables. Returns rows written."""
    total = 0
    for table_name in FACT_TABLES:
        if table_name not in fact_dfs:
            continue
        frame = fact_dfs[table_name]
        if table_name == "fact_environment":
            for column in ("value", "location_id"):
                if column not in frame.columns:
                    continue
                before = len(frame)
                frame = frame.dropna(subset=[column])
                if len(frame) < before:
                    logger.info("    dropped %d row(s) with null %s",
                                before - len(frame), column)
        if frame.empty:
            logger.warning("No valid rows to load for %s", table_name)
            continue
        total += loader.load_table(table_name, frame)
    return total


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def _filename_year(file_path: str) -> Optional[int]:
    """A 4-digit year from the file stem, if one is present.

    Read for one purpose only: to add a ``year`` column when the mapping expects
    one and the source does not carry it. If the source already has a ``year``
    column this value is ignored — see ``run_ingestion_for_source``.
    """
    stem = os.path.splitext(os.path.basename(file_path))[0]
    candidates = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", stem)
    return int(candidates[-1]) if candidates else None


def run_ingestion_for_source(source_config: Dict[str, Any],
                             context: RunContext) -> bool:
    """Run the complete ETL pipeline for one source."""
    source_name = source_config.get("source_name", "unknown")
    logger.info("===== Processing source: %s =====", source_name)
    try:
        file_paths = get_file_paths(source_config)
        if not file_paths:
            logger.warning("No files to process for %s", source_name)
            return False

        mapping_path = get_mapping_path(source_config)
        if not mapping_path and not source_config.get("columns"):
            raise ValueError(
                f"source {source_name!r} declares neither `matching:` nor "
                f"`columns:`; one is required to say which source column becomes "
                f"which CDM column.")

        loader = DBLoader(batch_size=10000, upsert=True, use_copy=True)
        extract_mode = source_config.get("extract_mode", "single")
        reader_args = dict(source_config.get("extension_options") or {})

        dim_rows = fact_rows = 0
        for file_path in file_paths:
            logger.info("Processing file: %s", file_path)
            year = _filename_year(file_path)

            extractor = FileExtractor(extract_mode=extract_mode,
                                      large_df_threshold=10_000_000)
            result = extractor.extract(file_path, **reader_args)

            chunks = result if extract_mode == "chunked" else [result]
            for chunk_index, chunk_df in enumerate(chunks, start=1):
                if chunk_df is None or chunk_df.empty:
                    continue
                if extract_mode == "chunked" and chunk_index % 10 == 0:
                    logger.info("  processed %d chunk(s) so far", chunk_index)

                if year is not None and "year" not in chunk_df.columns:
                    chunk_df = chunk_df.copy()
                    chunk_df["year"] = year
                elif year is not None:
                    logger.debug(
                        "  source already carries a `year` column; not overwriting "
                        "it with %s from the filename", year)

                dim_dfs = transform_dimensions(chunk_df, source_config,
                                               mapping_path, context)
                if dim_dfs:
                    dim_rows += load_dimensions(dim_dfs, loader)

                fact_dfs = transform_facts(chunk_df, source_config,
                                           mapping_path, context)
                if fact_dfs:
                    fact_rows += load_facts(fact_dfs, loader)

        logger.info("Source %r completed: %d dimension row(s), %d fact row(s)",
                    source_name, dim_rows, fact_rows)
        return True
    except Exception as exc:
        logger.error("Failed to process source %r: %s", source_name, exc,
                     exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------

def run_consolidation_step(core_cfg: Dict[str, Any],
                           context: RunContext) -> Dict[str, Any]:
    """Run the full consolidation step for this run."""
    logger.info("=" * 60)
    logger.info("RUNNING DATA CONSOLIDATION")
    logger.info("=" * 60)

    result = run_consolidation(
        core_cfg, registry=context.registry, run_id=context.run_id, close=True)

    if isinstance(result, dict) and result.get("error"):
        logger.error("Consolidation failed; summary generation is skipped.")
    else:
        _log_consolidation_summary(result)
    return result


def _log_consolidation_summary(results: Dict[str, Any]) -> None:
    """Emit a human-readable summary of a ``consolidate_all()`` result dict.

    Every key read here is one ``consolidate_all`` actually returns. An earlier
    revision read six keys that were never produced and printed zeros while the
    run reported success.
    """
    if not isinstance(results, dict):
        return

    logger.info("-" * 60)
    logger.info("CONSOLIDATION SUMMARY")
    logger.info("-" * 60)

    fk = results.get("foreign_key_alignment") or {}
    summary = (fk.get("summary") or {}) if isinstance(fk, dict) else {}
    logger.info("  Foreign keys:  %d row(s) repaired, %d orphan(s), %d quarantined",
                summary.get("total_fixed", 0), summary.get("orphans_found", 0),
                summary.get("quarantined", 0))

    loc = results.get("locations") or {}
    if isinstance(loc, dict):
        clusters = loc.get("cluster_results") or {}
        logger.info(
            "  Locations:     %d -> %d (%d merged across %d group(s) by "
            "standard_code), %d country code(s) filled, %d parent(s) repaired",
            loc.get("original_count", 0), loc.get("final_count", 0),
            clusters.get("merged_count", 0), clusters.get("group_count", 0),
            loc.get("country_codes_filled", 0),
            (loc.get("repaired_parents") or {}).get("total", 0))

        rebuilt = loc.get("hierarchy_rebuilt") or {}
        parents_resolved = loc.get("parents_resolved") or {}
        logger.info(
            "                 %d location(s) placed from standard_code, "
            "%d placed by the spatial pass, %d grid cell(s) total",
            rebuilt.get("placed", 0),
            parents_resolved.get("placed_by_geometry", 0),
            loc.get("grid_count", 0))

        grid_parents = loc.get("grid_parents") or {}
        if isinstance(grid_parents, dict) and grid_parents.get("grid_cells"):
            logger.info(
                "                 %d/%d grid cell(s) parented at %r "
                "(%d recognised by name pattern, %d without coordinates)",
                grid_parents.get("assigned", 0), grid_parents.get("grid_cells", 0),
                grid_parents.get("admin_level"),
                grid_parents.get("matched_by_pattern", 0),
                grid_parents.get("no_coordinates", 0))
        elif isinstance(grid_parents, dict):
            logger.warning("                 no grid cell was parented (%s): %s",
                           grid_parents.get("status"), grid_parents.get("reason")
                           or "the R analysis cannot reach a study-area cell "
                              "until this is fixed")

        same_name = loc.get("name_duplicate_groups") or []
        if same_name:
            logger.info("                 %d same-name group(s) kept as distinct "
                        "places, e.g. %s", len(same_name),
                        ", ".join(f"{g['name']}×{g['count']}"
                                  for g in same_name[:4]))

    vload = results.get("vocabulary_load") or {}
    if isinstance(vload, dict) and vload.get("packs"):
        logger.info("  Vocabularies:  %d loaded (%d concept(s)), %d already "
                    "present, %d without a CSV",
                    vload.get("loaded", 0), vload.get("concepts", 0),
                    vload.get("skipped", 0), vload.get("no_csv", 0))
        for pack_id, entry in sorted(vload["packs"].items()):
            if entry.get("status") in ("no_csv", "error"):
                logger.warning("    %-12s %s%s — every value in domain %r stays "
                               "unresolved",
                               pack_id, entry.get("status"),
                               (f": {entry['reason']}" if entry.get("reason") else ""),
                               entry.get("domain"))
            elif entry.get("status") == "loaded":
                logger.info("    %-12s %d concept(s) from %s", pack_id,
                            entry.get("concepts", 0),
                            Path(entry.get("csv") or "?").name)

    for key, stats in sorted((results.get("vocabulary_standard") or {}).items()):
        if isinstance(stats, dict):
            logger.info("  Standardised %s: %d applied of %d (%d exact, %d "
                        "normalized, %d alias, %d fuzzy), %d needs review, "
                        "%d unresolved", key, stats.get("applied", 0),
                        stats.get("total", 0), stats.get("exact", 0),
                        stats.get("normalized", 0), stats.get("alias", 0),
                        stats.get("fuzzy", 0), stats.get("needs_review", 0),
                        stats.get("unresolved", 0))

    dim_align = results.get("value_alignment") or {}
    fact_align = results.get("fact_value_alignment") or {}
    if dim_align or fact_align:
        rewritten = [
            f"{entry['table']}.{entry['column']}={entry['updated']}"
            for entry in list((dim_align.get("fields") or {}).values()) +
                               list((fact_align.get("fields") or {}).values())
            if isinstance(entry, dict) and entry.get("updated")
        ]
        logger.info("  Value align:   %d dimension row(s) + %d fact row(s) "
                    "renamed to the canonical concept name%s",
                    dim_align.get("updated", 0), fact_align.get("updated", 0),
                    (f" ({', '.join(rewritten[:6])})" if rewritten else ""))

    report = results.get("alignment_report") or {}
    ungoverned = report.get("ungoverned") or []
    if ungoverned:
        logger.warning("  %d field(s) have NO governing vocabulary; values kept "
                       "exactly as written and merged on a normalised key only:",
                       len(ungoverned))
        for entry in ungoverned[:12]:
            logger.warning("    %-50s %d distinct", entry["field"],
                           entry["distinct_values"])
        if len(ungoverned) > 12:
            logger.warning("    ... and %d more", len(ungoverned) - 12)

    for table, stats in sorted((results.get("vocabulary_consolidate") or {}).items()):
        if isinstance(stats, dict) and stats.get("duplicates_found"):
            logger.info("  Merged %d duplicate group(s) in %s",
                        stats["duplicates_found"], table)

    vcons = results.get("merge") or {}
    total_deleted = sum((r or {}).get("deleted", 0) for r in vcons.values()
                        if isinstance(r, dict))
    if total_deleted:
        logger.info("  Merge pass:    %d dimension row(s) removed", total_deleted)

    orphans = results.get("remaining_orphans") or {}
    if orphans:
        logger.warning("  Remaining orphans: %d FK column(s)", len(orphans))
        for key, count in sorted(orphans.items()):
            logger.warning("    %s: %d", key, count)

    validation = results.get("validation") or {}
    if validation:
        logger.info("  Country codes: %d valid, %d invalid, %d missing (of %d)",
                    validation.get("valid", 0), validation.get("invalid", 0),
                    validation.get("missing", 0), validation.get("total", 0))

    hierarchy = results.get("hierarchy") or {}
    if hierarchy:
        logger.info("  Hierarchy:     %d/%d non-admin location(s) parented",
                    hierarchy.get("placed", 0), hierarchy.get("total_non_admin", 0))

    burden = results.get("burden") or {}
    if burden:
        logger.info("  Burden:        %d daily row(s) inserted, %d skipped, "
                    "%d no-population, %d error(s)", burden.get("inserted", 0),
                    burden.get("skipped", 0), burden.get("no_population", 0),
                    burden.get("errors", 0))

    resistance = results.get("resistance") or {}
    if resistance and not resistance.get("status"):
        logger.info("  Resistance:    %d classified, %d unclassified (cut-offs "
                    "%s/%s)", resistance.get("classified", 0),
                    resistance.get("unclassified", 0),
                    (resistance.get("thresholds") or {}).get("susceptible"),
                    (resistance.get("thresholds") or {}).get("suspected"))

    duplicates = results.get("duplicates") or {}
    for table, stats in sorted(duplicates.items()):
        count = (stats or {}).get("duplicate_count", 0)
        if count:
            logger.warning("  %s: %d fact duplicate group(s) collapsed", table, count)

    if results.get("conflicts"):
        logger.warning("  C7 conflicts:  %d location row(s) carried a concept the "
                       "spatial pass did not produce", results["conflicts"])

    close_report = results.get("close") or {}
    if isinstance(close_report, dict):
        if close_report.get("flushed"):
            logger.info("  Resolutions:   %d flushed into the ledger",
                        close_report["flushed"])
        for message in close_report.get("errors") or ():
            logger.warning("  Resolution drain: %s", message)

    logger.info("-" * 60)


def run_burden_expansion(force: bool = True) -> bool:
    """Expand yearly burden rows into daily case records."""
    logger.info("=" * 60)
    logger.info("RUNNING BURDEN EXPANSION")
    logger.info("=" * 60)

    consolidator = None
    try:
        consolidator = DataConsolidator()
        stats = consolidator.expand_burden_to_daily(force=force)
        logger.info("Burden expansion: %d inserted, %d present, %d skipped, "
                    "%d no-population, %d error(s)", stats["inserted"],
                    stats["already_present"], stats["skipped"],
                    stats["no_population"], stats["errors"])
        return True
    except Exception as exc:  
        logger.error("Burden expansion failed: %s", exc, exc_info=True)
        return False
    finally:
        if consolidator is not None:
            consolidator.close()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def generate_summary(output_dir: str = "outputs") -> None:
    logger.info("Generating database summary...")
    output_path = PROJECT_ROOT / output_dir
    output_path.mkdir(exist_ok=True)
    schema_summary_to_csv(str(output_path / "schema_summary.csv"))
    export_all_table_samples(str(output_path / "table_samples"), rows_per_table=100)
    logger.info("Summary generated in %s", output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="CDM Integration Framework")
    parser.add_argument("--config", default="base_config.yaml")
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--skip-summary", action="store_true")
    parser.add_argument("--skip-consolidation", action="store_true")
    parser.add_argument("--expand-burden", action="store_true")
    parser.add_argument("--source", help="Run only for this source key")
    parser.add_argument("--dry-run", action="store_true",
                        help="Consolidate without writing")
    args = parser.parse_args()

    setup_environment()

    config_path = PROJECT_ROOT / args.config
    if not config_path.exists():
        logger.error("Configuration file not found: %s", config_path)
        sys.exit(1)

    config = load_yaml_config(str(config_path)) or {}
    logger.info("Loaded configuration from %s", config_path)

    core = config.get("core") or {}
    steps = (config.get("process") or {}).get("steps", [])

    context = RunContext(countries=list(core.get("countries") or []))
    logger.info("run id %s | registry %s | %d pack(s)",
                context.run_id, context.registry_digest[:12] or "(none)",
                len(context.registry.packs()) if context.registry else 0)
    logger.info("database %s (file: %s)", DATABASE_URL,
                engine.url.database or "(in memory)")

    run_ingest = not args.skip_ingest and "ingest_data" in steps
    run_consolidate = not args.skip_consolidation and "consolidate" in steps
    run_summary = not args.skip_summary and "generate_summary" in steps

    if run_ingest:
        data_sources = config.get("data_sources") or {}
        if not data_sources:
            logger.warning("No data sources defined in configuration.")
        else:
            for source_key, source_cfg in data_sources.items():
                if args.source and source_key != args.source:
                    continue
                run_ingestion_for_source(source_cfg, context)

    if run_consolidate:
        result = run_consolidation_step(core, context)
        if isinstance(result, dict) and result.get("error"):
            if run_summary:
                logger.error("Skipping summary: consolidation failed.")
            sys.exit(2)

    if run_summary:
        generate_summary()

    if args.expand_burden and not run_consolidate:
        run_burden_expansion()

    logger.info("Run %s complete.", context.run_id)


if __name__ == "__main__":
    main()