#!-*- coding: utf8 -*-


#!-*- coding: utf8 -*-

# Mapping from NetCDF variable name to database column and conversion
NC_VARIABLE_MAP = {
    't2m': {
        'column': 'temperature_mean',
        'convert': lambda k: k - 273.15,   # Kelvin to Celsius
        'agg_method': 'mean',
        'description': '2m temperature'
    },
    'tp': {
        'column': 'rainfall_total',
        'convert': lambda m: m * 1000,  # meters to mm
        'agg_method': 'sum',
        'description': 'Total precipitation'
    },
}

# Vector Atlas vector occurrence mapping
VECTOR_OBSERVATION_MAPPING = {
    '__latitude__': 'site_latitude',
    '__longitude__': 'site_longitude',
    '__year__': 'year_start',
    '__month__': 'month_start',
    '__count__': 'sample_occurrence_n_tot',
    'vector_species': 'recordedSpecies_species',
}

# GBIF vector observation mapping
GBIF_VECTOR_OBSERVATION_MAPPING = {
    # Core required fields
    '__latitude__': 'decimalLatitude',
    '__longitude__': 'decimalLongitude',
    '__year__': 'year',
    '__month__': 'month',
    '__count__': 'individualCount',
    '__event_date_full__': 'eventDate',  # Full date for fallback
    
    # Species information
    'vector_species': 'scientificName',
    'vector_subspecies': 'infraspecificEpithet',  # Optional subspecies info
    
    # Additional GBIF fields that might be useful
    'collection_method': 'basisOfRecord',  # Maps to collection method type
    
    # Note: These fields are not typically in GBIF, so they're omitted
    # 'life_stage': None,
    # 'sex': None,
    # 'tested_for_pathogen': None,
    # 'infection_status': None,
    # 'infection_rate': None,
}


# Case data mapping example
CASE_DATA_MAPPING = {
    'case_identifier': 'case_id',
    'location_code': 'district',
    'date_id_onset': 'onset_date',
    'date_id_reported': 'report_date',
    'disease': 'disease',
    'age_years': 'age',
    'sex': 'gender',
    'outcome': 'outcome',
    '__constants__': {
        'disease': 'malaria',
        'host_type': 'human',
    }
}


VECTOR_ATLAS_MAPPING = {
    # ===== LOCATION COORDINATES =====
    # The dataset has multiple coordinate columns - using site_latitude/site_longitude as primary
    '__latitude__': 'site_latitude',                    # Primary latitude
    '__longitude__': 'site_longitude',                  # Primary longitude
    
    # Fallback coordinates if primary is missing
    '__latitude_fallback__': 'location_coordinates_0',  # Fallback latitude
    '__longitude_fallback__': 'location_coordinates_1', # Fallback longitude
    
    # Secondary coordinate system (if needed for verification)
    '__latitude_alt__': 'site_latitude_2',
    '__longitude_alt__': 'site_longitude_2',
    
    # ===== DATE INFORMATION =====
    '__year__': 'year_start',                            # Year of observation
    '__month__': 'month_start',                          # Month of observation
    
    # Optional date range (if sampling spanned multiple months)
    '__year_end__': 'year_end',
    '__month_end__': 'month_end',
    
    # ===== COUNT/ABUNDANCE DATA =====
    # Primary count column (total across all sampling events)
    '__count__': 'sample_occurrence_n_tot',
    
    # Individual sampling event counts
    '__count_1__': 'sample_occurrence_n_1',
    '__count_2__': 'sample_occurrence_n_2',
    '__count_3__': 'sample_occurrence_n_3',
    '__count_4__': 'sample_occurrence_n_4',
    
    # Sampling method descriptions
    '__sampling_1__': 'sample_sampling_occurrence_1',
    '__sampling_2__': 'sample_sampling_occurrence_2',
    '__sampling_3__': 'sample_sampling_occurrence_3',
    '__sampling_4__': 'sample_sampling_occurrence_4',
    
    # ===== VECTOR OBSERVATION FIELDS =====
    # These map directly to your VectorObservation model
    
    # Species identification
    'vector_species': 'recordedSpecies_species',        # Primary species name
    'vector_subspecies': 'species',                     # Secondary species identifier
    
    # Presence/absence
    'binary_presence': 'binary_presence',
    
    # Collection metadata
    'collection_method': 'dataset_dataType',            # Type of data collection
    'life_stage': 'bionomics_adult_data',               # Adult/larval data
    
    # Infection/parasite data
    'tested_for_pathogen': 'bionomics_infection',
    'infection_status': {
        'source': 'bionomics_infection',
        'function': lambda x: 'positive' if x and 'positive' in str(x).lower() 
                              else 'negative' if x and 'negative' in str(x).lower()
                              else 'unknown'
    },
    
    # ===== SITE METADATA =====
    'site_name': 'site_site',
    'country': 'site_country',
    'admin_level_1': 'site_admin_level_1',
    'admin_level_2': 'site_admin_level_2',
    'location_type': 'site_location_type',
    'georef_confidence': 'site_confidence_in_georef',
    'georef_source': 'site_georef_source',
    'area_type': 'site_area_type',
    
    # ===== DATASET METADATA =====
    'dataset_id': 'id',
    'dataset_doi': 'dataset_doi',
    'dataset_title': 'dataset_title',
    'dataset_source': 'dataset_dataSource',
    'dataset_description': 'dataset_description',
    'dataset_location': 'dataset_location',
    'dataset_region': 'dataset_region',
    
    # ===== BIONOMICS DATA =====
    'bionomics_adult': 'bionomics_adult_data',
    'bionomics_larval': 'bionomics_larval_site_data',
    'bionomics_sampling_design': 'bionomics_study_sampling_design',
    'bionomics_season': 'bionomics_season_given',
    'bionomics_rainfall': 'bionomics_rainfall_time',
    'biting_rate': 'bionomics_bitingRate',
    'anthropo_zoophagic': 'bionomics_anthropoZoophagic',
    'endo_exophagic': 'bionomics_endoExophagic',
    'biting_activity': 'bionomics_bitingActivity',
    'environment_type': 'bionomics_environment',
    'larval_site': 'bionomics_LarvalSite',
    
    # ===== REFERENCE INFORMATION =====
    'reference_author': 'reference_author',
    'reference_title': 'reference_article_title',
    'reference_journal': 'reference_journal_title',
    'reference_citation': 'reference_citation',
    'reference_year': 'reference_year',
    'reference_published': 'reference_published',
    
    # ===== RESISTANCE DATA (for ResistanceTest table) =====
    'resistance': {
        'test_type': 'insecticideResistanceBioassays_test_protocal',
        'insecticide': 'insecticideResistanceBioassays_insecticide_tested',
        'insecticide_class': 'insecticideResistanceBioassays_insecticide_class',
        'irac_moa': 'insecticideResistanceBioassays_irac_moa',
        'irac_moa_code': 'insecticideResistanceBioassays_irac_moa_code',
        'concentration_percent': 'insecticideResistanceBioassays_concentration_percent',
        'concentration_microgram': 'insecticideResistanceBioassays_concentration_microgram',
        'exposure_period': 'insecticideResistanceBioassays_exposure_period_min',
        'intensity_multiplier': 'insecticideResistanceBioassays_intensity_multiplier',
        'synergist_tested': 'insecticideResistanceBioassays_synergist_tested',
        'synergist_concentration': 'insecticideResistanceBioassays_synergist_concentration',
        'synergist_unit': 'insecticideResistanceBioassays_synergist_concentration_unit',
        'mosquitoes_tested': 'insecticideResistanceBioassays_mosquitors_tested_n',
        'mosquitoes_dead': 'insecticideResistanceBioassays_mosquitors_dead_n',
        'percent_mortality': 'insecticideResistanceBioassays_percent_mortality',
        'knockdown_time': 'insecticideResistanceBioassays_knock_down_expo_time_min',
        'knockdown_count': 'insecticideResistanceBioassays_no_mosq_knock_down',
        'knockdown_percent': 'insecticideResistanceBioassays_knock_down_percent',
        'ktd50': 'insecticideResistanceBioassays_ktd_50_percent_min',
        'ktd90': 'insecticideResistanceBioassays_ktd_90_percent_min',
        'ktd95': 'insecticideResistanceBioassays_ktd_95_percent_min',
        'bioassay_notes': 'insecticideResistanceBioassays_bioassay_notes',
        'generation': 'insecticideResistanceBioassays_generation',
        'wild_caught': 'insecticideResistanceBioassays_wild_caught_larvae_adults',
    },
    
    # ===== MOLECULAR DATA (KDR mutations etc.) =====
    'molecular': {
        'vgsc_method': 'insecticideResistanceBioassays_vgscMethodAndSample',
        'kdr_genotypes': 'insecticideResistanceBioassays_kdrGenotypeFrequencies',
        'vgsc995_allele': 'insecticideResistanceBioassays_vgsc995AlleleFrequencies',
        'vgsc402_genotypes': 'insecticideResistanceBioassays_vgsc402GenotypeFrequencies',
        'vgsc402_allele': 'insecticideResistanceBioassays_vgsc402AlleleFrequencies',
        'cyp6aap_allele': 'insecticideResistanceBioassays_cyp6aapAlleleFrequencies',
        'cyp6aap_genotypes': 'insecticideResistanceBioassays_cyp6aapGenotypeFrequencies',
        'cyp6p4_allele': 'insecticideResistanceBioassays_cyp6p4AlleleFrequencies',
        'cyp6p4_genotypes': 'insecticideResistanceBioassays_cyp6p4GenotypeFrequencies',
        'cyp4j5_allele': 'insecticideResistanceBioassays_cyp4j5AlleleFrequencies',
        'cyp4j5_genotypes': 'insecticideResistanceBioassays_cyp4j5GenotypeFrequencies',
        'gste2_119_allele': 'insecticideResistanceBioassays_gste2_119AlleleFrequencies',
        'gste2_119_genotypes': 'insecticideResistanceBioassays_gste2_119GenotypeFrequencies',
        'gste2_114_allele': 'insecticideResistanceBioassays_gste2_114AlleleFrequencies',
        'gste2_114_genotypes': 'insecticideResistanceBioassays_gste2_114GenotypeFrequencies',
        'rdl_method': 'insecticideResistanceBioassays_rdlMethodAndSample',
        'rdl296_genotypes': 'insecticideResistanceBioassays_rdl296GenotypeFrequencies',
        'rdl296_allele': 'insecticideResistanceBioassays_rdl296AlleleFrequencies',
        'ace1_method': 'insecticideResistanceBioassays_ace1MethodAndSample',
        'ace1_genotypes': 'insecticideResistanceBioassays_ace1GenotypeFrequencies',
        'ace1_allele': 'insecticideResistanceBioassays_ace1AlleleFrequencies',
    }
}


VECTOR_ATLAS_SIMPLE_MAPPING = {
    # Primary coordinates (site_latitude/site_longitude exist in your data)
    '__latitude__': 'site_latitude',
    '__longitude__': 'site_longitude',
    
    # Fallback coordinates (if primary is missing)
    '__latitude_fallback__': 'location_coordinates_0',
    '__longitude_fallback__': 'location_coordinates_1',
    
    # Date information
    '__year__': 'year_start',
    '__month__': 'month_start',
    
    # Count - use total first, then sum individual counts
    '__count__': 'sample_occurrence_n_tot',
    '__count_alt__': ['sample_occurrence_n_1', 'sample_occurrence_n_2', 
                      'sample_occurrence_n_3', 'sample_occurrence_n_4'],
    
    # Vector species
    'vector_species': 'recordedSpecies_species',
    
    # Optional: infection data
    'tested_for_pathogen': 'bionomics_infection',
    
    # Site name for reference
    'site_name': 'site_site',
}

