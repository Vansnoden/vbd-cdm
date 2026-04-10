


## usage examples

Run all default steps (vocabulary, location, environmental, vectors)

```python main.py```

Run only vocabulary and location, using a specific study area

```python main.py --steps vocabulary,location --study-area siaya```

Reset database (deletes everything) then run full pipeline

```python main.py --reset-db```

Only backfill country names for existing locations

```python main.py --backfill-countries```

Use a custom config file

```python main.py --config my_config.yaml --steps environmental```