#!/usr/bin/env python
# scripts/load_locations.py
import sys
from pathlib import Path
# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import geopandas as gpd
from database.session import SessionLocal
from database.models import Location


def load_gpkg_to_location(gpkg_path, admin_level, layer=None, parent_code=None):
    """
    Reads a GeoPackage file and inserts each feature into the location table.
    :param gpkg_path: Path to the .gpkg file
    :param admin_level: e.g., 'county', 'subcounty'
    :param layer: Layer name within the GeoPackage (if multiple layers)
    :param parent_code: Optional parent location code for hierarchical data
    """
    # Read the GeoPackage (GeoPandas automatically detects format)
    gdf = gpd.read_file(gpkg_path, layer=layer)
    
    # Ensure CRS is WGS84 (EPSG:4326)
    if gdf.crs and gdf.crs.to_string() != 'EPSG:4326':
        gdf = gdf.to_crs('EPSG:4326')
    
    session = SessionLocal()
    try:
        for _, row in gdf.iterrows():
            # Try to get a name from common attribute fields
            # Adjust these field names to match your actual data
            name = None
            for col in ['NAME', 'name', 'COUNTY', 'county', 'ADMIN', 'admin', 'adm1_name']:
                if col in row and row[col]:
                    name = str(row[col])
                    break
            if not name:
                name = f"Feature_{_}"  # fallback
            
            # Generate a unique location code (e.g., lowercase with underscores)
            location_code = name.lower().replace(' ', '_').replace('-', '_')
            
            # Get geometry as WKT
            geom_wkt = row.geometry.wkt if row.geometry else None
            
            # Compute centroid
            if row.geometry:
                centroid = row.geometry.centroid
                centroid_lat = centroid.y
                centroid_lon = centroid.x
            else:
                centroid_lat = centroid_lon = None
            
            existing = session.query(Location).filter_by(location_code=location_code).first()
            if existing:
                # Update existing record
                existing.name = name
                existing.admin_level = admin_level
                existing.parent_location_code = parent_code
                existing.geometry_wkt = geom_wkt
                existing.centroid_lat = centroid_lat
                existing.centroid_lon = centroid_lon
                print(f"Updated: {location_code}")
            else:
                # Insert new record
                location = Location(
                    location_code = location_code,
                    name = name,
                    admin_level = admin_level,
                    parent_location_code = parent_code,
                    geometry_wkt = geom_wkt,
                    centroid_lat = centroid_lat,
                    centroid_lon = centroid_lon
                )
                session.add(location)
                print(f"Inserted: {location_code}")

        session.commit()
        print(f"Loaded {len(gdf)} locations from {gpkg_path}")
    except Exception as e:
        session.rollback()
        print(f"Error loading GeoPackage: {e}")
    finally:
        session.close()


if __name__ == "__main__":
    # Define paths to your .gpkg files
    base = Path(__file__).parent / "study_areas" / "kenya"
    mombasa_gpkg = base / "mombasa_county" / "mombasa_county.gpkg"   # adjust filename
    siaya_gpkg = base / "siaya_county" / "siaya_county.gpkg"

    # Load each county
    if mombasa_gpkg.exists():
        load_gpkg_to_location(mombasa_gpkg, admin_level='county')
    if siaya_gpkg.exists():
        load_gpkg_to_location(siaya_gpkg, admin_level='county')