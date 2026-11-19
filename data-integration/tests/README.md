# path: tests/README.md
# status: NEW FILE
# Golden-file regression fixtures

## What they are

`tests/fixtures/golden/` holds a CSV snapshot of **every** `dim_*` and `fact_*`
table in the CDM, captured by running the real pipeline and nothing else.

`tests/test_backward_compat.py` does the following, in order:

1. `tests/conftest.py` binds `DATABASE_URL` to a throw-away SQLite file in a
   temporary directory before any application module is imported, then creates
   the schema from `Base.metadata`. Your working `vbd_cdm.db` and any
   PostgreSQL instance in `.env` are never opened.
2. A synthetic data set is provisioned in the same temporary directory: two
   sources (`vectoratlas`, `irmapper_ir_1`), one mapping CSV per source, and a
   `base_config.yaml` whose `process.steps` are `ingest_data` and `consolidate`.
   The fixture data lives outside version control on purpose — changing it
   shows up as a golden diff instead of as an invisible edit.
3. For each source in order, the test invokes `main.main()` through its CLI
   entry point (it does not reimplement the orchestrator), with `--source`
   selecting that source and consolidation enabled.
4. After each source, every `dim_*` and `fact_*` table is written to
   `<source>_<table>.csv`. After the final source, `fact_concept_map` is
   written once to `fact_concept_map.csv`.

On every subsequent run the same files are produced and compared. Any
difference fails the test with a unified diff.

## Why they exist

The design review's invariant 5 says the refactor must be *measurable*. A
golden baseline is the measurement: it records the exact CDM the current code
produces, so a later change either reproduces it or produces a diff a reviewer
can read. Without it, "the concept pass did not change anything" is a claim;
with it, it is a test.

The snapshots are deliberately taken through the whole pipeline rather than at
one seam. Batch 1 rewrites the persistence layer, batch 2 the resolver, batch 3
the registry, batch 4 the schema. A test that only pinned `fact_concept_map`
would miss a change to how a fact table is rewritten after a dimension merge.

## What is normalised, and why

A golden file must be reproducible or it is worthless. The writer
(`scripts/snapshot_golden.py`) does four things before writing:

- **Volatile columns are blanked.** `loaded_at`, `mapped_at`, `run_id` and
  similar record *when* something happened, not *what* was decided. A wall
  clock in a golden file fails every run.
- **Surrogate keys that depend on insertion order are blanked.**
  `fact_concept_map.map_id` is the current example: the consolidator reads
  `SELECT DISTINCT` without an `ORDER BY`, so the ids can permute without any
  behavioural change. This is the review's finding C9, visible in the baseline
  rather than hidden by it.
- **Rows are sorted.** By primary key normally, and by the natural key
  `(source_table, source_column, source_value)` for `fact_concept_map` once its
  surrogate is blanked.
- **Line endings are `\n`, the index is never written.** The script and the
  test share one writer, so files cannot diverge between the two paths.

`dim_time` is seeded for 1900-01-01 to 2026-12-31 by `main.setup_environment()`,
so it contributes roughly 2 MB per source snapshot. It is included because the
specification is "every `dim_*` and `fact_*` table", and because a change to
the time dimension would otherwise be invisible.

## How to regenerate

After an intended behaviour change:

    UPDATE_GOLDEN=1 pytest tests/test_backward_compat.py

This overwrites the files in `tests/fixtures/golden/` and logs each one. Commit
the regenerated files in the same commit as the code change, so the diff and
its cause are reviewable together.

`UPDATE_GOLDEN` also accepts `true`, `yes` and `on`.

To inspect what the writer would emit for an existing database without running
pytest:

    python scripts/snapshot_golden.py --db-path vbd_cdm.db
    python scripts/snapshot_golden.py --db-path vbd_cdm.db \
        --output-dir /tmp/golden --prefix irmapper_ir_1_

The script writes `<prefix><table>.csv` for every CDM table and logs each table
name with its row count. With the last source's prefix it reproduces that
source's files exactly. It cannot reproduce the intermediate state captured
after *earlier* sources, because that state no longer exists in a finished
database; use `UPDATE_GOLDEN` for those.

## When a diff is expected: "explainably better"

A diff is not a failure to be silenced. It is a claim that the new output is
better, and a claim has to be argued. A diff is explainably better when the
pull request states all four of:

1. **Which invariant it serves.** "Disease names now collapse to one concept
   code" serves invariant 2 (determinism). "The fuzzy threshold moved from one
   global 0.85 to a per-vocabulary value" serves invariant 3.
2. **Which mechanism produced it.** Name the function or the file. "Fuzzy
   tie-breaking is now `(score, concept_rank, concept_code)` in
   `ConceptResolver`" is a mechanism.
3. **The direction, and why it is the right direction.** "`match_method` moves
   from `none` to `normalized` for three of five vector labels, because the
   normaliser now strips the genus prefix" says what changed and why that is
   the correct outcome.
4. **Why nothing was lost.** "Row counts are unchanged for
   `fact_vector_observation`; only `dim_vector.concept_id` moved" is the
   sentence that proves the change is not a regression.

"This looks fine" is not an argument. Neither is "the golden file was stale".
A regenerated baseline with no accompanying explanation is treated as a test
failure that happened to be committed.

Three shapes of diff and what they mean:

- **Row count changes** in a fact table. Always a data-loss or duplication
  question first, a feature question second. The review's §6 names destructive
  dimension merges as the largest risk in this refactor.
- **`match_method` and `decision` value changes** in `fact_concept_map`. This
  is the expected diff for batches 1 to 3, and it should be accompanied by the
  number of values that moved band.
- **Column-level changes**, such as `is_approved` becoming NULL where it was
  `0`. Expected for batch 4's schema migration, and the explanation is the
  migration's own rationale: NULL means "nobody has reviewed this", which is
  not the same as "a human rejected it".

## The contract with batches 1 to 4

- No pull request is merged while a golden diff is unexplained. The diff is
  reviewed like any other code change.
- A batch that intends to change output regenerates the fixtures in the same
  pull request and describes the change using the four points above.
- A batch that does not intend to change output must leave these files
  byte-identical. A diff there is a bug in the batch, not a fixture update.
- If a diff is unexpected, do not regenerate. Revert the code, find the cause,
  and only then decide whether the new behaviour is correct.
- Batches 1 to 4 must keep the *shape* of the harness intact: the same entry
  point (`main.main()`), the same writer (`scripts/snapshot_golden.py`), and
  the same file naming. If the harness itself has to change, that change is
  part of the pull request and is called out explicitly.

## Self-check
- [x] Every file in scope present: `tests/conftest.py`, `tests/test_backward_compat.py`, `scripts/snapshot_golden.py`, `tests/README.md`
- [x] No forbidden placeholder tokens
- [x] Imports complete
- [x] Interface contract respected (no drift)

## Notes on what I changed or assumed

1. **`<source>_<table>.csv` is read as per-source cumulative.** The spec writes `<source>_<table>.csv` and then names `fact_concept_map.csv` without a prefix, so I treated the prefix as real. Each source gets a snapshot of the whole database after that source's run, and `fact_concept_map` is captured once at the end rather than duplicated per source. If you meant a flat `<table>.csv`, the change is a one-line edit in the test loop.
2. **`scripts/snapshot_golden.py` gained an optional `--prefix`.** Without it the README's "regenerate by running the script" instruction cannot reproduce the test's file names, which would have made two writers disagree about what a baseline is. This is an addition beyond `--db-path` and `--output-dir`, not a change to them.
3. **The test imports its writer from `scripts/`.** A single `render_csv`/`write_csv` implementation is what stops `UPDATE_GOLDEN` and the CLI from producing non-identical files. The alternative — one copy in `conftest.py` and one in the script — would drift on the first edit.
4. **Summary generation is excluded.** `generate_summary()` writes into `<repo>/outputs/`, which cannot be redirected, and never touches the CDM. It is omitted from the fixture config's `process.steps` and skipped on the command line. Everything that mutates the database runs.
5. **`fact_concept_map.map_id` is masked, `fact_concept_map` is sorted by its natural key.** This is forced by C9 in the review: `SELECT DISTINCT` without `ORDER BY` means the surrogate ids already permute run to run, so an unmasked baseline would be flaky before batch 1 lands. Masking it means the baseline pins the *decisions*, which is what batches 1–4 change.
6. **Unverified by execution.** I could not run the suite in this environment. The fixture is deliberately shaped around known sharp edges in the current code — `location_code` is supplied because `_prepare_for_upsert` collapses all-NULL natural keys to one row, the fixture file stems contain no four-digit number because `main.py` scrapes a year from the filename, and site coordinates are tens of kilometres apart so DBSCAN does not merge them — but the first `UPDATE_GOLDEN=1` run is where that gets confirmed. The README says so explicitly: the baseline does not exist until someone runs it once and commits the result.
