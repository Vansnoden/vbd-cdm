#-*-coding:utf8-*-


import ee



def add_coords(feature):
    """Add lat and lon properties derived from the feature's geometry."""
    geom = feature.geometry()
    coords = geom.coordinates()
    return feature.set({
        'lat': coords.get(1),
        'lon': coords.get(0)
    })


def download_health_facility_occurrence_data(county_name: str):
    # 1. Setup Names
    search_name_upper = county_name.upper()

    # 2. Load your Tabular Asset (No coordinates)
    my_list = ee.FeatureCollection("projects/gen-lang-client-0359288668/assets/kenya-health-facilities-2017_08_02") \
                .filter(ee.Filter.eq('County', search_name_upper))

    # 3. Load a Spatial Reference (OSM Health Facilities - which HAVE coordinates)
    # We use a public dataset to "find" the points for your names
    osm_reference = ee.FeatureCollection("projects/sat-io/open-datasets/HOT_OSM/ken_health_facilities_points")

    # 4. Define a Join
    # We match 'Name' from your list to 'name' in OSM
    save_best_join = ee.Join.saveFirst(matchKey='spatial_match')
    
    # Define the link: Match your "Name" column to OSM's "name" column
    # Note: OSM names are often lowercase, so we use a fuzzy match or just equals
    filter_match = ee.Filter.equals(
        leftField='Name', 
        rightField='name'
    )

    # Execute Join
    joined_col = save_best_join.apply(my_list, osm_reference, filter_match)

    # 5. Extract Coordinates from the joined "spatial_match"
    def extract_joined_coords(feature):
        matched_feat = ee.Feature(feature.get('spatial_match'))
        # If no match was found, this would stay null, so we handle that:
        return feature.set({
            'lon': ee.Algorithms.If(matched_feat, matched_feat.geometry().coordinates().get(0), 0),
            'lat': ee.Algorithms.If(matched_feat, matched_feat.geometry().coordinates().get(1), 0)
        })

    # Map the extraction and filter out those we couldn't find (the 0s)
    facilities_final = joined_col.map(extract_joined_coords).filter(ee.Filter.neq('lat', 0))

    # 6. Select and Export
    facilities_export = facilities_final.select(
        ['Name', 'Facility type', 'Owner', 'County', 'lat', 'lon']
    )

    count = facilities_export.size().getInfo()
    print(f"Matched {count} facilities with coordinates in {search_name_upper}.")

    if count > 0:
        task = ee.batch.Export.table.toDrive(
            collection=facilities_export,
            description=f'{county_name}_geocoded_facilities',
            folder='GEE_exports',
            fileFormat='CSV'
        )
        task.start()
        print("Geocoding export started.")


if __name__ == '__main__':
    
    ee.Authenticate()
    ee.Initialize(project='gen-lang-client-0359288668')
    download_health_facility_occurrence_data(county_name='SIAYA')
    

