# VBD-CDM Analysis

R-side analysis of the vector-borne disease Common Data Model produced by the
Python ingestion pipeline in `../data-integration/`.

> **Vector-centred.** Vectors (*Anopheles* spp.) are the study subject.
> Environmental, resistance, bionomics, and case data are all interpreted in
> relation to them. The pipeline is designed to run even when
> `fact_disease_case` is empty.

## Where to start

| I want to ... | Read |
|---|---|
| Run the pipeline for the first time | [Getting started](getting-started.md) |
| Change the study area or time window | [Configuration](configuration.md) |
| Understand a figure in `outputs/analysis/` | [Analysis modules](analysis-modules.md) |
| Add a new analysis | [Pipeline -> extending](pipeline.md#extending) |
| Debug an error | [Troubleshooting](troubleshooting.md) |
| Know what's in the database | [Data access](data-access.md) |

## Contents

- [Getting started](getting-started.md)
- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [Data access](data-access.md)
- [Pipeline](pipeline.md)
- [Analysis modules](analysis-modules.md)
- [Plots and reports](plots-and-reports.md)
- [Troubleshooting](troubleshooting.md)
- [Glossary](glossary.md)