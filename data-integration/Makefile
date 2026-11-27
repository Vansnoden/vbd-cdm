# One-command entry points. Nothing here is required by the code: main.py and
# scripts/*.py run standalone. These targets exist so a reader can reproduce the
# paper's figures and the test suite without reading the docs first.
RECIPEPREFIX = >

PYTHON ?= python
DB ?= vbd_cdm.db
CONFIG ?= base_config.yaml

.PHONY: help install test lint cov demo golden er docs snapshot samples export \
        clean fix-cases resistance

help:
> @echo "install     pip install -e '.[dev,geo,raster,stats]'"
> @echo "test        pytest"
> @echo "cov         pytest with coverage, term + html report"
> @echo "lint        ruff check"
> @echo "demo        alembic upgrade head, then run the configured pipeline"
> @echo "golden      regenerate tests/fixtures/golden from \$$DB"
> @echo "er          render er_diagram_publication.png"
> @echo "docs        build the sphinx docs into docs/_build/html"
> @echo "snapshot    export every CDM table of \$$DB to outputs/snapshot"
> @echo "samples     schema summary + per-table samples into outputs/"
> @echo "export      one wide CSV of every fact table into outputs/"
> @echo "fix-cases   burden-expansion preflight (read-only)"
> @echo "resistance  recompute resistance_status, then verify"
> @echo "clean       remove build/run artefacts (never the database)"

install:
> $(PYTHON) -m pip install --upgrade pip
> $(PYTHON) -m pip install -e '.[dev,geo,raster,stats]'

test:
> $(PYTHON) -m pytest -q

cov:
> $(PYTHON) -m pytest -q --cov --cov-report=term-missing --cov-report=html

lint:
> -$(PYTHON) -m ruff check .

demo:
> $(PYTHON) -m alembic upgrade head
> $(PYTHON) main.py --config $(CONFIG)

golden:
> $(PYTHON) scripts/snapshot_golden.py --db-path $(DB)

er:
> $(PYTHON) scripts/generate_er_neo.py

docs:
> $(PYTHON) -m sphinx -b html docs docs/_build/html

snapshot:
> $(PYTHON) scripts/snapshot_golden.py --db-path $(DB) --output-dir outputs/snapshot

samples:
> $(PYTHON) -c "from utilities.database_summary import schema_summary_to_csv, export_all_table_samples; \
> schema_summary_to_csv('outputs/schema_summary.csv'); \
> export_all_table_samples('outputs/table_samples', rows_per_table=100)"

export:
> $(PYTHON) scripts/export_to_csv.py --out outputs/cdm_export.csv

fix-cases:
> $(PYTHON) scripts/fix_cases.py --check

resistance:
> $(PYTHON) scripts/fix_resistance_status.py

clean:
> rm -rf build dist .pytest_cache .ruff_cache htmlcov .coverage docs/_build
> find . -name '__pycache__' -type d -prune -exec rm -rf {} +
> @echo "kept: $(DB), logs/, outputs/"