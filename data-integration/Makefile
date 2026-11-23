.PHONY: test golden lint
PYTHON ?= python
DB ?= /tmp/scratch.db

test:
	$(PYTHON) -m compileall -q .
	$(PYTHON) -m pytest -q

golden:
	cp vbd_cdm.db $(DB)
	SQL_LITE_DB_NAME=$(DB) $(PYTHON) scripts/snapshot_golden.py \
		--db-path $(DB) --output-dir tests/fixtures/golden

lint:
	ruff check . && black --check .