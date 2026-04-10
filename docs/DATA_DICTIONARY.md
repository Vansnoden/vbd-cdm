| Table | Field | Type | Description |
|-------|-------|------|-------------|
| **DIM_TIME** | date_id | DATE | Primary key. Unique date identifier in YYYY-MM-DD format. |
| | year | INT | Calendar year. |
| | month | INT | Calendar month (1–12). |
| | week | INT | ISO week number. |
| | day_of_year | INT | Day number within the year (1–366). |
| | season | VARCHAR | Season name (e.g., 'dry', 'wet', 'spring'). |
| | is_holiday | BOOLEAN | Whether the date is a public holiday (true/false). |
| **DIM_LOCATION** | location_code | VARCHAR | Primary key. Unique code for the location (e.g., 'gh_greater_accra'). |
| | name | VARCHAR | Location name (e.g., 'Greater Accra', 'Siaya'). |
| | admin_level | VARCHAR | Administrative level (e.g., 'country', 'region', 'district', 'site'). |
| | parent_location_code | VARCHAR | Foreign key to the parent location (e.g., region for a district). |
| | geometry_wkt | TEXT | Well‑Known Text representation of the location’s boundary or point. |
| | centroid_lat | FLOAT | Latitude of the location’s centroid (WGS84). |
| | centroid_lon | FLOAT | Longitude of the location’s centroid (WGS84). |
| | country_name | VARCHAR | Name of the country where the location resides. |
| | country_code | VARCHAR | ISO 3166‑1 alpha‑2 country code (e.g., 'GH', 'KE'). |
| **DIM_DISEASE** | disease_code | VARCHAR | Primary key. Short code for the disease (e.g., 'MAL', 'DEN'). |
| | disease_name | VARCHAR | Full disease name (e.g., 'Malaria', 'Dengue'). |
| | description | VARCHAR | Optional description of the disease. |
| | source_standard | VARCHAR | Reference to external standard (e.g., 'ICD-10:B54'). |
| | pathogen | VARCHAR | Causal pathogen (e.g., 'Plasmodium falciparum', 'Dengue virus'). |
| | icd_code | VARCHAR | ICD‑10 or ICD‑11 code for the disease. |
| **DIM_VECTOR** | vector_code | VARCHAR | Primary key. Unique code for the vector (e.g., 'ANGAM', 'AEDAE'). |
| | species_name | VARCHAR | Scientific species name (e.g., 'Anopheles gambiae', 'Aedes aegypti'). |
| | description | VARCHAR | Optional description. |
| | source_standard | VARCHAR | Reference to external standard (e.g., 'Darwin Core'). |
| | subspecies | VARCHAR | Subspecies, if applicable (e.g., 'gambiae', 'aegypti'). |
| | genus | VARCHAR | Genus name (e.g., 'Anopheles', 'Aedes'). |
| | family | VARCHAR | Family name (e.g., 'Culicidae'). |
| **DIM_INSECTICIDE** | insecticide_code | VARCHAR | Primary key. Short code for the insecticide (e.g., 'PYR', 'ORG'). |
| | insecticide_name | VARCHAR | Full name (e.g., 'Pyrethroid', 'Organophosphate'). |
| | description | VARCHAR | Optional description. |
| | source_standard | VARCHAR | Reference (e.g., 'IRAC:3A'). |
| | chemical_class | VARCHAR | Chemical class (e.g., 'Pyrethroid', 'Carbamate'). |
| | irac_group | VARCHAR | IRAC group classification for mode of action. |
| **DIM_IDENTIFICATION_METHOD** | method_code | VARCHAR | Primary key. Code for diagnostic method (e.g., 'PCR', 'RDT'). |
| | method_name | VARCHAR | Full method name (e.g., 'Polymerase chain reaction', 'Rapid diagnostic test'). |
| | description | VARCHAR | Optional description. |
| | source_standard | VARCHAR | Reference (e.g., 'LOINC', 'WHO'). |
| | method_type | VARCHAR | Broader category (e.g., 'molecular', 'serological', 'microscopy'). |
| **DIM_RESISTANCE_TEST_TYPE** | test_type_code | VARCHAR | Primary key. Code for bioassay type (e.g., 'WHO', 'CDC'). |
| | test_type_name | VARCHAR | Full name (e.g., 'WHO tube test', 'CDC bottle bioassay'). |
| | description | VARCHAR | Optional description. |
| | source_standard | VARCHAR | Reference (e.g., 'WHO', 'CDC'). |
| | exposure_time_unit | VARCHAR | Unit of exposure time (e.g., 'minutes', 'hours'). |
| **DIM_SOCIOECONOMIC_INDICATOR** | indicator_code | VARCHAR | Primary key. Code for indicator (e.g., 'ITN_USE', 'POVERTY'). |
| | indicator_name | VARCHAR | Full name (e.g., 'Insecticide‑treated net use', 'Poverty rate'). |
| | description | VARCHAR | Optional description. |
| | source_standard | VARCHAR | Reference (e.g., 'UNICEF', 'World Bank'). |
| | unit | VARCHAR | Measurement unit (e.g., '%', 'USD', 'count'). |
| **DIM_SOURCE** | source_code | VARCHAR | Primary key. Short code for the data source (e.g., 'GBIF', 'VA', 'WHO'). |
| | source_name | VARCHAR | Full name of the source (e.g., 'Global Biodiversity Information Facility'). |
| | source_description | VARCHAR | Brief description of the source. |
| | source_standard | VARCHAR | Reference standard or organisation. |
| | source_url | VARCHAR | URL to the source homepage or documentation. |
| | version | VARCHAR | Version of the dataset (e.g., '2024-01', 'v2.1'). |
| **FACT_VECTOR_OBSERVATION** | observation_id | INT | Primary key. Unique identifier for each vector surveillance record. |
| | location_code | VARCHAR | Foreign key to `DIM_LOCATION`. Point location where vector was collected. |
| | date_id | DATE | Foreign key to `DIM_TIME`. Date of observation. |
| | vector_code | VARCHAR | Foreign key to `DIM_VECTOR`. Identifies the vector species/subspecies. |
| | source_code | VARCHAR | Foreign key to `DIM_SOURCE`. Originating data source. |
| | life_stage | VARCHAR | Life stage observed (e.g., 'egg', 'larva', 'pupa', 'adult'). |
| | sex | VARCHAR | Sex of the vector ('male', 'female', 'mixed'). |
| | collection_method | VARCHAR | Method used to collect the vector (e.g., 'CDC light trap', 'BG sentinel'). |
| | count | INT | Number of vectors collected in the sample. |
| | density_per_trap | FLOAT | Calculated density (e.g., vectors per trap per night). |
| | tested_for_pathogen | VARCHAR | Pathogen for which the sample was tested (e.g., 'Plasmodium falciparum'). |
| | infection_status | VARCHAR | Result of pathogen test ('positive', 'negative', 'unknown'). |
| | infection_rate | FLOAT | Proportion of positive vectors in the sample (0–1). |
| **FACT_DISEASE_CASE** | case_id | INT | Primary key. Unique case identifier (can be external ID). |
| | location_code | VARCHAR | Foreign key to `DIM_LOCATION`. Location of patient residence or case report. |
| | date_id_onset | DATE | Foreign key to `DIM_TIME`. Date of symptom onset. |
| | date_id_reported | DATE | Foreign key to `DIM_TIME`. Date the case was reported to authorities. |
| | disease_code | VARCHAR | Foreign key to `DIM_DISEASE`. Disease diagnosed. |
| | identification_method_code | VARCHAR | Foreign key to `DIM_IDENTIFICATION_METHOD`. Method used for confirmation. |
| | source_code | VARCHAR | Foreign key to `DIM_SOURCE`. Originating data source. |
| | case_status | VARCHAR | Status of the case ('suspected', 'probable', 'confirmed'). |
| | host_type | VARCHAR | Type of host ('human', 'animal'). |
| | age_years | INT | Age of the patient in years. |
| | age_group | VARCHAR | Age category (e.g., '0–4', '5–14', '15+'). |
| | sex | VARCHAR | Sex of the patient ('male', 'female', 'other', 'unknown'). |
| | outcome | VARCHAR | Final outcome ('recovered', 'died', 'unknown'). |
| **FACT_INSECTICIDE_RESISTANCE** | resistance_id | INT | Primary key. Unique identifier for each resistance test. |
| | vector_observation_id | INT | Foreign key to `FACT_VECTOR_OBSERVATION`. Links to the vector sample tested. |
| | insecticide_code | VARCHAR | Foreign key to `DIM_INSECTICIDE`. Insecticide tested. |
| | test_type_code | VARCHAR | Foreign key to `DIM_RESISTANCE_TEST_TYPE`. Type of bioassay used. |
| | source_code | VARCHAR | Foreign key to `DIM_SOURCE`. Originating data source. |
| | concentration | FLOAT | Concentration of insecticide used in the test (e.g., µg/mL, %). |
| | exposure_time | INT | Duration of exposure (in units defined by test type). |
| | mortality | FLOAT | Proportion of vectors dead after exposure (0–1). |
| | resistance_status | VARCHAR | Interpreted status ('susceptible', 'confirmed resistance', 'possible resistance'). |
| **FACT_BIONOMICS** | bionomics_id | INT | Primary key. Unique identifier for bionomics record. |
| | vector_observation_id | INT | Foreign key to `FACT_VECTOR_OBSERVATION`. Links to the vector sample. |
| | source_code | VARCHAR | Foreign key to `DIM_SOURCE`. Originating data source. |
| | feeding_preference | VARCHAR | Feeding preference ('anthropophagic', 'zoophagic', 'mixed'). |
| | biting_location | VARCHAR | Location of host contact ('endophagic', 'exophagic', 'mixed'). |
| | resting_behavior | VARCHAR | Where the vector rests after feeding (e.g., 'indoor', 'outdoor'). |
| | larval_site_type | VARCHAR | Type of breeding site (e.g., 'puddle', 'container', 'rice field'). |
| **FACT_INTERVENTION** | intervention_id | INT | Primary key. Unique identifier for each intervention campaign. |
| | location_code | VARCHAR | Foreign key to `DIM_LOCATION`. Geographic area where intervention was deployed. |
| | date_start | DATE | Foreign key to `DIM_TIME`. Start date of the intervention. |
| | date_end | DATE | Foreign key to `DIM_TIME`. End date of the intervention (if applicable). |
| | target_disease_code | VARCHAR | Foreign key to `DIM_DISEASE`. Disease the intervention aims to control. |
| | source_code | VARCHAR | Foreign key to `DIM_SOURCE`. Originating data source. |
| | intervention_type | VARCHAR | Type of intervention (e.g., 'IRS', 'ITN', 'larviciding', 'vaccination'). |
| | coverage | FLOAT | Coverage achieved (e.g., percentage of houses sprayed, nets distributed). |
| **FACT_SOCIOECONOMIC_CONDITION** | socio_id | INT | Primary key. Unique identifier for each socioeconomic record. |
| | location_code | VARCHAR | Foreign key to `DIM_LOCATION`. Area to which the indicator applies. |
| | year | INT | Year of the measurement. |
| | indicator_code | VARCHAR | Foreign key to `DIM_SOCIOECONOMIC_INDICATOR`. The specific indicator. |
| | source_code | VARCHAR | Foreign key to `DIM_SOURCE`. Originating data source. |
| | indicator_value | FLOAT | Numeric value of the indicator (e.g., 42.5 for 42.5% ITN use). |
| **FACT_HEALTH_FACILITY** | facility_id | INT | Primary key. Unique identifier for the health facility. |
| | facility_code | VARCHAR | External code from national master facility list. |
| | location_code | VARCHAR | Foreign key to `DIM_LOCATION`. Administrative region where facility is located. |
| | source_code | VARCHAR | Foreign key to `DIM_SOURCE`. Originating data source. |
| | name | VARCHAR | Name of the facility. |
| | lat | FLOAT | Latitude of the facility (WGS84). |
| | lon | FLOAT | Longitude of the facility (WGS84). |
| | type | VARCHAR | Facility type (e.g., 'hospital', 'clinic', 'dispensary'). |
| | catchment_population | INT | Estimated population served by the facility. |
| **FACT_DISEASE_BURDEN** | burden_id | INT | Primary key. Unique identifier for each burden estimate. |
| | location_code | VARCHAR | Foreign key to `DIM_LOCATION`. Geographic area of the estimate. |
| | year | INT | Year of the estimate. |
| | disease_code | VARCHAR | Foreign key to `DIM_DISEASE`. Disease for which burden is estimated. |
| | source_code | VARCHAR | Foreign key to `DIM_SOURCE`. Originating data source. |
| | incidence_rate | FLOAT | Number of new cases per 1,000 population per year. |
| | prevalence_rate | FLOAT | Proportion of population infected at a given time (0–1). |
| | mortality_rate | FLOAT | Number of deaths per 1,000 population per year. |
| | cases_mean | FLOAT | Mean estimated number of cases (absolute count). |
| **FACT_ENVIRONMENTAL** | fact_env_id | INT | Primary key. Unique identifier for each environmental record. |
| | location_code | VARCHAR | Foreign key to `DIM_LOCATION`. Area for which the variable is measured. |
| | date_id | DATE | Foreign key to `DIM_TIME`. Date of measurement. |
| | variable | VARCHAR | Name of the environmental variable (e.g., 'temperature_mean', 'rainfall_total'). |
| | value | FLOAT | Measured value in standard units (e.g., °C, mm). |
| | source_code | VARCHAR | Foreign key to `DIM_SOURCE`. Originating data source (e.g., 'ERA5'). |
