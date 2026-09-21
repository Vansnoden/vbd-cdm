# Data model

<a id="erd"></a>

## Entity-relationship diagram

![image](../../../er_diagram.png)

## Layout

The CDM is a **star-schema** warehouse:

* **Conformed dimensions** - shared across many facts.
* **Domain facts** - one per observation family.
* **Bridge** - `fact_concept_map` for vocabulary standardisation.

#### Dimensions

| Table                           | PK                             | Notes                                                                                       |
|---------------------------------|--------------------------------|---------------------------------------------------------------------------------------------|
| `dim_time`                      | `date_id`                      | Pre-populated 1900-01-01 … 2026-12-31 by `main.init_dim_time()`.                            |
| `dim_location`                  | `location_id`                  | Autoincrement. `location_code` is the natural key. Grid cells carry `admin_level = 'grid'`. |
| `dim_source`                    | `source_code`                  | Provenance. Every fact row carries one.                                                     |
| `dim_vector`                    | `vector_code`                  | Species/subspecies. Joined to facts via `vector_code`.                                      |
| `dim_disease`                   | `disease_code`                 | ICD-10 aligned where possible.                                                              |
| `dim_insecticide`               | `insecticide_code`             | IRAC group where possible.                                                                  |
| `dim_identification_method`     | `method_code`                  | `'ESTIMATE'` is the synthetic-method code.                                                  |
| `dim_resistance_test_type`      | `test_type_code`               | WHO tube, CDC bottle, …                                                                     |
| `dim_socioeconomic_indicator`   | `indicator_code`               | World Bank indicator codes, e.g. `SP.POP.TOTL`.                                             |
| `dim_vocabulary`, `dim_concept` | `vocabulary_id` / `concept_id` | External vocabularies (GADM, IDOMAL, NCBI, MIRO).                                           |

#### Facts

| Table                          | PK                | Cardinality vs. dimensions                                                                                   |
|--------------------------------|-------------------|--------------------------------------------------------------------------------------------------------------|
| `fact_vector_observation`      | `observation_id`  | ↔ `dim_location`, `dim_time`, `dim_vector`, `dim_source`                                                     |
| `fact_environment`             | `fact_env_id`     | ↔ `dim_location`, `dim_time`, `dim_source`                                                                   |
| `fact_insecticide_resistance`  | `resistance_id`   | ↔ `dim_location`, `dim_time`, `dim_vector`, `dim_insecticide`, `dim_resistance_test_type`, `dim_source`      |
| `fact_bionomics`               | `bionomics_id`    | ↔ `dim_location`, `dim_time`, `dim_vector`, `dim_source`                                                     |
| `fact_disease_case`            | `case_id`         | ↔ `dim_location`, `dim_time` (×2: onset, reported), `dim_disease`, `dim_identification_method`, `dim_source` |
| `fact_disease_burden`          | `burden_id`       | ↔ `dim_location`, `dim_disease`, `dim_source`. Yearly.                                                       |
| `fact_intervention`            | `intervention_id` | ↔ `dim_location`, `dim_time` (×2), `dim_disease`, `dim_source`                                               |
| `fact_socioeconomic_condition` | `socio_id`        | ↔ `dim_location`, `dim_socioeconomic_indicator`, `dim_source`. Yearly.                                       |
| `fact_health_facility`         | `facility_id`     | ↔ `dim_location`, `dim_source`                                                                               |

## Full schema

| [`database.models.DimTime`](generated/database.models.DimTime.md#database.models.DimTime)                                                          | Time dimension table                                                   |
|----------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------|
| [`database.models.DimLocation`](generated/database.models.DimLocation.md#database.models.DimLocation)                                              | Location hierarchy (country, region, district, site)                   |
| [`database.models.DimSource`](generated/database.models.DimSource.md#database.models.DimSource)                                                    | Data source provenance                                                 |
| [`database.models.DimVector`](generated/database.models.DimVector.md#database.models.DimVector)                                                    | Vector species/subspecies vocabulary                                   |
| [`database.models.DimDisease`](generated/database.models.DimDisease.md#database.models.DimDisease)                                                 | Disease vocabulary with standard alignment                             |
| [`database.models.DimInsecticide`](generated/database.models.DimInsecticide.md#database.models.DimInsecticide)                                     | Insecticide vocabulary                                                 |
| [`database.models.DimIdentificationMethod`](generated/database.models.DimIdentificationMethod.md#database.models.DimIdentificationMethod)          | Diagnostic method vocabulary                                           |
| [`database.models.DimResistanceTestType`](generated/database.models.DimResistanceTestType.md#database.models.DimResistanceTestType)                | Resistance bioassay type vocabulary                                    |
| [`database.models.DimSocioeconomicIndicator`](generated/database.models.DimSocioeconomicIndicator.md#database.models.DimSocioeconomicIndicator)    | Socioeconomic indicator vocabulary                                     |
| [`database.models.FactVectorObservation`](generated/database.models.FactVectorObservation.md#database.models.FactVectorObservation)                |                                                                        |
| [`database.models.FactEnvironment`](generated/database.models.FactEnvironment.md#database.models.FactEnvironment)                                  | Environmental data (temperature, rainfall, etc.) stored in long format |
| [`database.models.FactInsecticideResistance`](generated/database.models.FactInsecticideResistance.md#database.models.FactInsecticideResistance)    |                                                                        |
| [`database.models.FactBionomics`](generated/database.models.FactBionomics.md#database.models.FactBionomics)                                        |                                                                        |
| [`database.models.FactDiseaseCase`](generated/database.models.FactDiseaseCase.md#database.models.FactDiseaseCase)                                  |                                                                        |
| [`database.models.FactDiseaseBurden`](generated/database.models.FactDiseaseBurden.md#database.models.FactDiseaseBurden)                            |                                                                        |
| [`database.models.FactIntervention`](generated/database.models.FactIntervention.md#database.models.FactIntervention)                               |                                                                        |
| [`database.models.FactSocioeconomicCondition`](generated/database.models.FactSocioeconomicCondition.md#database.models.FactSocioeconomicCondition) |                                                                        |
| [`database.models.FactHealthFacility`](generated/database.models.FactHealthFacility.md#database.models.FactHealthFacility)                         |                                                                        |
