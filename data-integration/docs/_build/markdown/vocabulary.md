# Vocabulary standardisation

Standardisation answers one question per raw value: *which standard concept does
this value mean, and on what evidence?*  Every answer is a record, not a side
effect. The decision, the vocabulary release that produced it, the resolver
version that made it and the runners-up that were considered all live in the
database, so a value can still be explained a year later.

Nothing about vocabularies is configured in a source’s YAML. Identity, release,
normalisation and thresholds belong to a **pack**, and packs live under
`vocabularies/registry/`.

> ##### On this page
> 
> * [The registry](#the-registry)
> * [Vocabulary identity](#vocabulary-identity)
> * [What standardisation reads and writes](#what-standardisation-reads-and-writes)
> * [The ladder](#the-ladder)
> * [The four decisions](#the-four-decisions)
> * [Where the decisions are stored](#where-the-decisions-are-stored)
> * [Reviewing decisions](#reviewing-decisions)
> * [Where a decision is applied](#where-a-decision-is-applied)
> * [Normalisers](#normalisers)
> * [Manifest keys](#manifest-keys)
> * [Building the vocabularies](#building-the-vocabularies)
> * [Loading](#loading)
> * [Troubleshooting](#troubleshooting)

## The registry

Each pack is one directory holding one `manifest.yaml`:

```default
vocabularies/registry/
├── gadm/manifest.yaml
├── idomal/manifest.yaml
├── ncbi/manifest.yaml
└── who-resistance/manifest.yaml
```

`utilities.vocabulary.registry.VocabularyRegistry.load_from` walks the tree for
`manifest.yaml` in sorted path order and builds two kinds of pack from the same
walk:

| Kind            | What it declares                                                                                                                                                                                                                                                                        |
|-----------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `ConceptPack`   | Concepts, a `domain`, a `field_scope`, a `normalizer_chain`, the<br/>matcher thresholds (`fuzzy_threshold`, `fuzzy_margin`), a rank<br/>(`precedence`, `specificity`), and optionally a boundary layout<br/>(`layers`, `admin_level_map`, `grid_level`) or<br/>`resistance_thresholds`. |
| `ParameterPack` | *Values* a policy needs, and nothing to match against: no domain, no<br/>field scope, no thresholds. `who-resistance` declares<br/>`resistance_thresholds` and is read by<br/>`utilities.consolidation._resistance_thresholds`.                                                         |

A parameter pack is deliberately not a `ConceptPack`. A concept pack with no
concepts would still have to declare a domain and a field scope, which would put
it into `field_bindings()` and make the resolver try to match against an empty
vocabulary.

### Shipped packs

| `pack_id`        | Kind      | Domain     | Dimension      | Source                                                                                                                                                                                                                                                                                                                |
|------------------|-----------|------------|----------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `gadm`           | concept   | `location` | `dim_location` | Administrative boundaries from a GADM `.gpkg`: one concept per<br/>administrative unit, keyed by GID, with centroid and `VARNAME_*` as<br/>synonyms. Declares the layer layout (`layers`, `admin_level_map`) that<br/>`utilities.admin_alignment` reads, and the `grid_level` that marks a<br/>synthetic raster cell. |
| `idomal`         | concept   | `disease`  | `dim_disease`  | IDOMAL malaria ontology (OBO): term IDs, definitions, synonyms.                                                                                                                                                                                                                                                       |
| `ncbi`           | concept   | `vector`   | `dim_vector`   | NCBI Taxonomy: the subtree under each genus in `VECTOR_TAXA`.                                                                                                                                                                                                                                                         |
| `who-resistance` | parameter | —          | —              | The WHO mortality cut-offs used to classify a bioassay<br/>(`resistance_thresholds`).                                                                                                                                                                                                                                 |

## Vocabulary identity

A pack’s identity in the database is version-qualified:

```default
vocabulary_id = "<pack_id>@<version>"        # e.g. "gadm@4.1"
```

That single decision lets two releases of one product coexist in `dim_concept`
and `fact_concept_map` with no schema change. Consequences worth knowing:

* `VocabularyLoader.load_csv` refuses a `vocabulary_id` without an `@`. An
  unqualified id resolves nothing, because `ConceptResolver` filters
  `dim_concept` on it literally.
* `ConceptResolver` reads only the id the pack declares. Concepts stored under
  a legacy unversioned id (`'GADM'`) are *invisible*; the resolver warns about
  this by name when a vocabulary comes back empty.
* `dim_vocabulary` therefore holds one row per *release*, not per product.

## What standardisation reads and writes

### `dim_vocabulary` — one row per release

| Column          | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                |
|-----------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `vocabulary_id` | `pack@version`. Primary key.                                                                                                                                                                                                                                                                                                                                                                                                           |
| `version`       | The release string. Never written as NULL by either writer.                                                                                                                                                                                                                                                                                                                                                                            |
| `pack_id`       | The unversioned grouping key the registry selects on (`gadm`).                                                                                                                                                                                                                                                                                                                                                                         |
| `released_with` | The tool release that shipped this pack.                                                                                                                                                                                                                                                                                                                                                                                               |
| `checksum`      | SHA-256 of **an** artefact — and `extra` says which.                                                                                                                                                                                                                                                                                                                                                                                   |
| `extra`         | `{"checksum_artefact": "manifest" | "csv"}`. Two writers hash two<br/>different artefacts into one column: the registry hashes a pack<br/>*manifest*, `VocabularyLoader.load_csv` hashes a vocabulary *CSV*. A<br/>64-character digest whose artefact is unknown cannot answer “which<br/>knowledge produced this decision”. Rows written before the discriminator<br/>existed keep `extra = NULL`, which reads as *artefact unknown*. |

### `dim_concept` — one row per concept

| Column                                   | Meaning                                                                                                                                                                                                                                                     |
|------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `vocabulary_id`, `concept_code`          | Unique together (`uq_concept_vocab_code`).                                                                                                                                                                                                                  |
| `concept_name`                           | The preferred label — the string the exact and normalised rungs compare<br/>against.                                                                                                                                                                        |
| `concept_class`, `domain`, `parent_code` | Vocabulary-local class, the CDM domain, and the hierarchy link.                                                                                                                                                                                             |
| `extra`                                  | JSON object. Holds `synonyms` (list), `definition`,<br/>`centroid_lat` / `centroid_lon` (GADM) and `rank` (a vocabulary’s own<br/>tie-break between two concepts sharing a name).                                                                           |
| `concept_norm`                           | `pack.normalizer()(concept_name)`, computed by the loader with the pack’s<br/>own chain and indexed. Populated so the L2 (normalised) rung is<br/>**reproducible** from the stored key rather than from a second<br/>implementation of the same normaliser. |

## The ladder

`etl.transformers.concept_resolver.ConceptResolver` tries six rungs in this
order; the first that matches decides. Nothing is added, removed or reordered,
and there is no seventh rung: `taxonomy_rollup` is deferred.

| Rung         | Score   | Decision        | Accepted when                                                                                                        |
|--------------|---------|-----------------|----------------------------------------------------------------------------------------------------------------------|
| `exact`      | 1.00    | `auto_accepted` | `concept_name` equals the probe (case-folded unless the pack sets<br/>`case_sensitive`).                             |
| `normalized` | 0.98    | `auto_accepted` | `normalise(concept_name)` equals `normalise(probe)`.                                                                 |
| `alias`      | 0.95    | `auto_accepted` | A synonym in `extra.synonyms` equals the probe, exactly or normally.                                                 |
| `fuzzy`      | ratio   | `auto_accepted` | `ratio >= pack.fuzzy_threshold` **and** the best candidate clears<br/>`pack.fuzzy_margin` over the runner-up.        |
| `ambiguous`  | ratio   | `needs_review`  | That same fuzzy rung with the **margin failing**: the match is recorded,<br/>but the decision withholds the concept. |
| `unresolved` | 0.00    | `unresolved`    | No rung matched (including a blank or `NULL` probe).                                                                 |

Two knobs, both read from the pack on every call: `fuzzy_threshold` and
`fuzzy_margin`. There is no default for either anywhere in the resolver —
inventing `0.85` in code is exactly the policy-as-literal the pack exists to
remove. `fuzzy_margin: 0` accepts an exact tie (still broken deterministically
by `concept_rank` then `concept_code`); a pack that wants ties flagged sets a
non-zero margin.

Determinism is a contract, not a preference:

* every query against `dim_concept` is `ORDER BY concept_code`;
* fuzzy candidates are ranked `(-score, -concept_rank, concept_code)`, never by
  row order, dict order or set order;
* the candidates JSON is serialised with `sort_keys=True` and fixed separators,
  so two runs produce byte-identical evidence.

## The four decisions

| Decision        | Meaning                                                                                                                                                      |
|-----------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `auto_accepted` | A concept was applied. The only decision that authorises stamping<br/>`concept_id` / `standard_code` or rewriting a value.                                   |
| `needs_review`  | A produced match whose band was not good enough to apply unsupervised, or<br/>an `auto_accepted` withheld by a pack’s `canonical_rule`. A human<br/>decides. |
| `rejected`      | A **human** verdict. Never produced by the resolver.                                                                                                         |
| `unresolved`    | No match at any rung.                                                                                                                                        |

`concept_code` is `None` for *every* decision that did not apply a concept —
`unresolved`, `rejected` and `ambiguous` alike. An `ambiguous` row’s
leaning is recorded in `candidates` and nowhere else. The guard every consumer
keys on is:

```default
apply the concept  <=>  resolution.decision == "auto_accepted"
```

## Where the decisions are stored

Two tables, two jobs. They are not duplicates.

### `fact_concept_map` — the **current** state

One row per `(source_table, source_column, source_value)`. Holds the decision
that is in force now, with its provenance:

| Column                                          | Meaning                                                                                                                                                             |
|-------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `source_table`, `source_column`, `source_value` | The key (`uq_concept_map_source`). Raw values are NA-normalised and<br/>trimmed before they are stored, so one decision has one key.                                |
| `vocabulary_id`, `vocabulary_version`           | The release that produced the decision.                                                                                                                             |
| `concept_code`, `match_method`, `match_score`   | What matched, by which rung, at what score (a proportion in `[0, 1]`).                                                                                              |
| `decision`, `resolver_version`, `last_run_id`   | The verdict, the resolver release that made it, and the run that last<br/>asserted it. Indexed on `decision`.                                                       |
| `is_approved`                                   | The **human** verdict, three-state: `NULL` = nobody has reviewed it,<br/>`TRUE` = a human approved, `FALSE` = a human rejected. No automated<br/>writer touches it. |
| `reviewed_by`, `reviewed_at`, `mapped_at`       | Who looked at it, when, and when the row was created. Preserved across<br/>re-runs.                                                                                 |

Only `auto_accepted` resolutions appear here. A withheld decision has no
applied concept, so a projection row claiming one would be a false statement
about the database’s state; the evidence stays in the ledger.

### `fact_resolution_log` — the **append-only** ledger

One row per `(source_table, source_column, source_value, run_id)`, for **every**
resolution including `unresolved` and `needs_review`. Writing is
insert-or-ignore against that key: re-running a run id is a no-op, while a new run
adds a new witness. Nothing in the codebase updates or deletes from this table.

It carries the full `Resolution` plus `candidates` (JSON, rank order) and
`resolved_at`. `run_id` is `NOT NULL` because it is part of the unique key —
SQL treats `NULL``s as distinct, so a nullable run id would silently permit
duplicate witnesses of one decision. There is deliberately no foreign key to
``dim_vocabulary`: the ledger must survive a vocabulary row being re-versioned
or deleted, and it records resolutions against packs that may not be loaded yet.

### A human review is protected

The projection into `fact_concept_map` carries a `WHERE` clause that refuses
to change `concept_code` on a row whose `is_approved = TRUE` and whose stored
code differs from the new one. Without it a re-run would silently re-decide a row
a reviewer approved while leaving `reviewed_by` / `reviewed_at` in place, and
attribute the new decision to that reviewer. The number of refusals is counted and
logged.

## Reviewing decisions

What still needs a human, from the ledger for one run:

```default
SELECT source_table, source_column, source_value, decision,
       match_method, match_score, candidates
FROM fact_resolution_log
WHERE run_id = :run_id
  AND decision <> 'auto_accepted'
ORDER BY source_column, match_score DESC;
```

The candidate the resolver leaned towards, with scores:

```default
SELECT source_value, j.value ->> 'code'   AS concept_code,
                   j.value ->> 'name'   AS concept_name,
                   j.value ->> 'score'  AS score
FROM fact_resolution_log, json_each(candidates) AS j
WHERE source_column = 'species_name' AND decision = 'needs_review'
ORDER BY score DESC;
```

To **approve** a decision, write the human verdict *and* its author. A verdict
without a reviewer is rejected by `database.schemas.FactConceptMapCreate`,
because it is indistinguishable from an automated one:

```default
UPDATE fact_concept_map
   SET is_approved = TRUE,
       reviewed_by = 's.atagong',
       reviewed_at = CURRENT_TIMESTAMP
 WHERE source_table = 'dim_vector'
   AND source_column = 'species_name'
   AND source_value = 'An. gambiae s.l.';
```

To **promote** a withheld decision, write the row yourself — it does not exist in
the projection yet — with the concept you chose and your name attached:

```default
INSERT INTO fact_concept_map
    (source_table, source_column, source_value, vocabulary_id,
     concept_code, match_method, match_score, decision,
     is_approved, reviewed_by, reviewed_at)
VALUES
    ('fact_bionomics', 'feeding_preference', 'anthropophilic',
     'idomal@2024-05', 'IDOMAL:0002045', 'ambiguous', 0.91,
     'auto_accepted', TRUE, 's.atagong', CURRENT_TIMESTAMP);
```

To **reject**, delete the projection row and record the verdict in the ledger by
re-running with the value excluded; a later run re-attempts the value and writes a
fresh ledger witness.

## Where a decision is applied

`utilities.vocabulary.field_catalog.FIELD_CATALOG` is the closed list of fields a
vocabulary may govern. It is derived from the schema, never from user
configuration, and a manifest’s `field_scope` entry that is not in it fails
loading.

| Field class                                                         | What happens to it                                                                                                                                                                                                                                                                                      |
|---------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `dim_*` label columns (`dim_vector.species_name`, …)                | `concept_id` and `standard_code` are stamped on the row, then<br/>`align_dimension_values` rewrites the label to the canonical concept<br/>name. `dim_location.country_code` is typed `iso3166_alpha2` and is<br/>standardised earlier, at stage A6a, from static ISO data rather than from a<br/>pack. |
| `fact_*` free-text columns (`fact_bionomics.feeding_preference`, …) | A fact table has no `concept_id`, so the decision lives only in<br/>`fact_concept_map` and `align_fact_values` rewrites the value in place<br/>through that map.                                                                                                                                        |
| A field with no governing pack                                      | Left exactly as the source wrote it and merged on a normalised key only.<br/>Reported: `alignment_report` lists every such field with its distinct<br/>count, so “nothing resolved” is visible rather than silent.                                                                                      |

One field is never renamed: `dim_location.name`. Boundary labelling stamps a
`standard_code` on every row whose centroid falls inside a polygon, including
survey sites and grid cells; a concept-driven rename would overwrite a site’s own
name with its district’s and destroy the identity the site exists to carry. A
location’s name is its own; its boundary membership is `standard_code`.

## Normalisers

`utilities.vocabulary.normalizers` is the only definition of these functions;
do not add a copy anywhere — two copies of a regex mean two answers to “is this
the same species”, and the one in the matcher wins by accident.

| id                 | Behaviour                                                                                                                                                                                                                                                                 |
|--------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `generic`          | Strip diacritics (NFKD + drop combining marks), collapse whitespace,<br/>lower-case. Used for diseases, insecticides, methods.                                                                                                                                            |
| `species_canon`    | Species-specific: case-fold, underscores/hyphens to space, drop the leading<br/>`anopheles`, drop a trailing `complex` / `subgroup` / `group` /<br/>`s.l.` / `s.s.` / `s form` / `m form`. Deliberately does **not**<br/>collapse hybrids (`Anopheles coluzzii/gambiae`). |
| `[]` (empty chain) | The identity function: exact matching only.                                                                                                                                                                                                                               |

A manifest names a chain by id (`normalizer_chain: [generic, species_canon]`);
`compose` applies it left to right. An unknown id fails at manifest load, not at
first match — a vocabulary whose `concept_norm` cannot be computed must not be
loaded, because a wrong normalised key silently widens the L2 rung for as long as
the row lives.

## Manifest keys

Required, for a concept pack:

| Key                         | Meaning                                                                                                                                              |
|-----------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| `pack_id`                   | Lower-case alphanumerics, `_`, `.`, `-`. Unique across **both** kinds.                                                                               |
| `version`                   | The release string, e.g. `4.1`.                                                                                                                      |
| `domain`                    | One of the catalog domains. Must agree with the domain the catalog gives<br/>every field in `field_scope`.                                           |
| `precedence`, `specificity` | The rank: `(precedence, -specificity, pack_id)`. Higher precedence wins;<br/>among equals the more specific pack wins; the id makes the order total. |
| `normalizer_chain`          | Ordered normaliser ids.                                                                                                                              |
| `fuzzy_threshold`           | In `(0, 1]`.                                                                                                                                         |
| `fuzzy_margin`              | In `[0, 1)`.                                                                                                                                         |
| `field_scope`               | The `FIELD_CATALOG` fields this pack may resolve. Empty is an error.                                                                                 |
| `released_with`             | The tool release that ships it.                                                                                                                      |

Optional, with the value used when absent:

| Key                     | Default   | Notes                                                                                                                                                                                                                                                                                                                        |
|-------------------------|-----------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `case_sensitive`        | `false`   | Applies to the exact and alias-exact rungs.                                                                                                                                                                                                                                                                                  |
| `applies_when`          | `{}`      | Only `countries`, `admin_level` and `sample_regex` are understood;<br/>anything else is a load error, not a silently ignored predicate.                                                                                                                                                                                      |
| `canonical_rule`        | `null`    | Comma-separated predicates. `never_collapse_hybrids` withholds a fuzzy<br/>match; `require_spatial_or_human` currently withholds everything;<br/>`prefer_genus_prefix` only biases which row is kept canonical. An<br/>unimplemented token makes every value it governs *not* auto-applied, and<br/>says so once in the log. |
| `layers`                | `()`      | `[{level, layer, gid_column, name_columns}, …]`. Unknown keys inside an<br/>entry are a load error: a typo there means reading the wrong column and<br/>silently mis-parenting a district.                                                                                                                                   |
| `admin_level_map`       | `{}`      | Integer boundary level to `dim_location.admin_level`. Declaring one<br/>without the other, or leaving a level unmapped, is a load error.                                                                                                                                                                                     |
| `grid_level`            | `null`    | The `admin_level` value that marks a synthetic grid cell.                                                                                                                                                                                                                                                                    |
| `resistance_thresholds` | `null`    | `{susceptible: <pct>, suspected: <pct>}`, each in `[0, 100]`, with<br/>`suspected <= susceptible`.                                                                                                                                                                                                                           |

Loading is all-or-nothing: a duplicate `pack_id`, a rank collision, or a
malformed manifest raises and the registry does not half-load — a registry that
half-loaded would make `digest()` describe a pack set that is not on disk.

`digest()` is the provenance stamp: SHA-256 over the sorted
`(pack_id, version)` pairs of **both** kinds, so a run that classified a bioassay
under different cut-offs cannot share a digest with one that did not. It is
layout-independent. `digest_with_kind()` adds the pack kind for the case where
the *kind* is what changed; the two are different namespaces and must not be
compared with each other.

## Building the vocabularies

The three concept CSVs are build artefacts, not source files. `build_all` writes
each one where the loader looks first —
`vocabularies/registry/<pack_id>/concepts.csv`:

```bash
# what exists today
python -m utilities.vocabulary.build_all --status

# boundary concepts from a GADM GeoPackage
python -m utilities.vocabulary.build_all --gpkg /data/gadm41_KEN.gpkg --country KEN

# disease concepts from an OBO ontology
python -m utilities.vocabulary.build_all --obo /data/idomal.obo

# vector species from NCBI Taxonomy (network access)
python -m utilities.vocabulary.build_all --ncbi --email you@example.org
```

`ncbi` is opt-in because it is a network fetch, rate-limited to three requests
per second without an API key.

Building a disease vocabulary directly, when it is not IDOMAL:

```default
python -m utilities.vocabulary.build diseases \
    --obo-path /data/mondo.obo \
    --output   vocabularies/registry/mondo/concepts.csv \
    --vocabulary-id MONDO \
    --ontology-prefix MONDO:
```

`--vocabulary-id` is **required** and is never inferred from the filename. A
build from `mondo.obo` used to be stamped `IDOMAL` because “idomal” was a
substring of the path; two vocabularies silently became one and the only way to
notice was to read the CSV. A path passed where an identity belongs is rejected
explicitly. An empty build raises rather than writing a header-only CSV: a
vocabulary with no concepts resolves nothing and reports success.

The CSV shape both builders agree on:

| Column                         | Notes                                                                                                                    |
|--------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| `concept_code`                 | Required. e.g. `GID_2_KEN.3_1`, `IDOMAL:0000657`, an NCBI taxid.                                                         |
| `concept_name`                 | Required. The preferred label.                                                                                           |
| `concept_class`                | Optional. `admin2`, `genus`, `species`, a namespace.                                                                     |
| `parent_code`                  | Optional. Hierarchy.                                                                                                     |
| `synonyms`                     | Optional, pipe-delimited (`"malaria|falciparum malaria"`). Read into<br/>`extra.synonyms` and matched by the alias rung. |
| `definition`                   | Optional. Read into `extra.definition`.                                                                                  |
| `centroid_lat`, `centroid_lon` | Optional. GADM emits them; read into `extra`.                                                                            |
| `rank`, `rank_weight`          | Optional. The vocabulary’s own tie-break between two concepts sharing a<br/>name; read into `extra.rank`.                |

`vocabulary_id` in the CSV is ignored on load. The pack is the only source of
identity, version, pack id, release and domain, because a vocabulary loaded under
an id the resolver does not query is invisible.

## Loading

Loading is part of the consolidation run, in this order:

1. `utilities.vocabulary.alignment.load_shipped_vocabularies` loads each pack’s
   CSV into `dim_concept`. It is idempotent by checksum: a pack whose stored
   `dim_vocabulary.checksum` already matches its CSV costs one hash and no
   writes. A pack with no CSV is *reported*, not fatal — the registry may describe
   a vocabulary the operator has not built yet.
2. `DataConsolidator.standardize_vocabularies` (stage A8) records the registry’s
   packs in `dim_vocabulary`, then reconciles every field the registry binds,
   writing decisions and stamping `concept_id` / `standard_code`.
3. `align_dimension_values` (A8b) and `align_fact_values` (A8c) rewrite values
   to the canonical concept name.
4. `alignment_report` prints per-field counts and every ungoverned field.

By hand, against a specific database:

```bash
# read-only: what the packs contain and what resolved
python -m utilities.vocabulary.alignment --report

# load the shipped CSVs, then rewrite values
python -m utilities.vocabulary.alignment --load --align

# rehearse without writing
python -m utilities.vocabulary.alignment --align --dry-run
```

The `--report` command refuses to run against a database holding no
`dim_location` and no `dim_source` rows. That is not a vocabulary problem — it
means the process is reading a different database from the one the pipeline wrote,
which is the single most expensive misdiagnosis available here.
`--allow-empty` overrides it.

Or from Python, one release at a time:

```python
from database.session import SessionLocal
from utilities.vocabulary.registry import VocabularyRegistry
from utilities.vocabulary.loader import VocabularyLoader

registry = VocabularyRegistry.load_from("vocabularies/registry")
pack = registry.get("idomal")          # a ConceptPack

session = SessionLocal()
try:
    written = VocabularyLoader(session).load_csv(
        "vocabularies/registry/idomal/concepts.csv", pack, dry_run=False)
    print(written, pack.vocabulary_id)  # e.g. 412 'idomal@2024-05'
finally:
    session.close()
```

## Troubleshooting

**Every value in a domain resolves to \`\`unresolved\`\`.** Two causes, both named in
the log:

* the vocabulary is empty — `pack 'gadm' (domain location) has no concepts to
  match against`. The CSV was never built, or was built and never loaded.
* the domain disagrees with the pack’s — the log prints the domains actually
  present for that `vocabulary_id` with counts, which shows the filter excluded
  everything.

A third, quieter cause: concepts stored under an unversioned id. The log names the
count and says to re-load through
`VocabularyLoader.load_csv(csv, registry.get(pack_id))`.

**\`\`fact_concept_map\`\` is empty but \`\`dim_concept\`\` has rows.** No pack claims the
field. `alignment_report` lists every ungoverned field with its distinct count.

**Checksums disagree between two writers.** Expected: the registry hashes a
manifest, the loader hashes a CSV. Read `dim_vocabulary.extra` —
`{"checksum_artefact": "manifest"|"csv"}` — before comparing. `NULL` means the
row predates the discriminator.

**Two packs claim one field.** `select_pack` ranks them by
`(precedence, -specificity, pack_id)` and the winner is deterministic. If two
packs share precedence *and* specificity, the winner is decided by the `pack_id`
string; that is stable but rarely intended, so it is logged with the winner named.
`registry.merge_policies()` states the resulting policy per field
(`primary`, `fallback`, the most conservative margin).

**A class of values is never auto-applied.** Check the pack’s
`canonical_rule`. `require_spatial_or_human` withholds every match, and
`never_collapse_hybrids` withholds fuzzy matches. An unimplemented token
withholds everything it governs and logs once, naming the pack.
