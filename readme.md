


## How to run

# Full pipeline
```python main.py```

# Skip ingestion, only regenerate summary
```python main.py --skip-ingest```

# Run only one source
```python main.py --source "climate_timeseries"```

# Skip analysis
```python main.py --skip-analysis```

# Update documenation
```sphinx-build -b html docs _build/html```