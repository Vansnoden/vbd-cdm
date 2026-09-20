
"""
Verify that all locations have valid ISO country codes.
"""

import sys
import os
from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from database.session import SessionLocal
from database.models import DimLocation
from database.geoutils import get_country_from_coords, get_iso_country_code, get_country_name
from sqlalchemy import text


def verify_country_codes():
    """Verify all locations have valid ISO country codes."""
    session = SessionLocal()
    
    try:
        # Get all locations
        locations = session.query(DimLocation).all()
        
        print(f"Total locations: {len(locations)}")
        
        # Statistics
        stats = {
            'total': len(locations),
            'has_code': 0,
            'valid_iso': 0,
            'invalid_iso': 0,
            'missing': 0,
            'has_coords': 0,
            'coords_no_country': 0
        }
        
        invalid_locations = []
        
        for loc in locations:
            if loc.country_code:
                stats['has_code'] += 1
                # Check if valid ISO code
                iso_code = get_iso_country_code(loc.country_code)
                if iso_code:
                    stats['valid_iso'] += 1
                else:
                    stats['invalid_iso'] += 1
                    invalid_locations.append({
                        'id': loc.location_id,
                        'name': loc.name,
                        'code': loc.country_code,
                        'issue': 'invalid_code'
                    })
            else:
                stats['missing'] += 1
                invalid_locations.append({
                    'id': loc.location_id,
                    'name': loc.name,
                    'code': None,
                    'issue': 'missing_code'
                })
            
            # Check coordinates
            if loc.centroid_lat and loc.centroid_lon:
                stats['has_coords'] += 1
                if not loc.country_code:
                    # Try to resolve from coordinates
                    name, code = get_country_from_coords(loc.centroid_lat, loc.centroid_lon)
                    if code:
                        invalid_locations.append({
                            'id': loc.location_id,
                            'name': loc.name,
                            'code': code,
                            'issue': 'can_be_fixed'
                        })
        
        print("\n=== Country Code Statistics ===")
        for key, value in stats.items():
            print(f"{key}: {value}")
        
        print(f"\nInvalid/Missing locations: {len(invalid_locations)}")
        
        # Show sample of invalid locations
        if invalid_locations:
            print("\nSample of locations needing attention:")
            for loc in invalid_locations[:10]:
                print(f"  ID: {loc['id']}, Name: '{loc['name']}', Issue: {loc['issue']}")
        
        # Show distribution of country codes
        code_dist = session.execute(text("""
            SELECT country_code, COUNT(*) as count
            FROM dim_location
            WHERE country_code IS NOT NULL AND country_code != ''
            GROUP BY country_code
            ORDER BY count DESC
        """)).fetchall()
        
        print("\n=== Country Code Distribution ===")
        for code, count in code_dist:
            print(f"  {code}: {count}")
        
        return stats, invalid_locations
        
    finally:
        session.close()


def fix_country_codes(dry_run=True):
    """Fix invalid or missing country codes."""
    session = SessionLocal()
    
    try:
        locations = session.query(DimLocation).filter(
            (DimLocation.country_code.is_(None)) |
            (DimLocation.country_code == '') |
            (DimLocation.country_code == 'UNKNOWN') |
            (DimLocation.country_code == 'N/A')
        ).all()
        
        print(f"Found {len(locations)} locations with missing country codes")
        
        fixed = 0
        for loc in locations:
            if loc.centroid_lat and loc.centroid_lon:
                name, code = get_country_from_coords(loc.centroid_lat, loc.centroid_lon)
                if code:
                    if not dry_run:
                        loc.country_code = code
                        loc.country_name = name
                    fixed += 1
                    print(f"  Fixed: {loc.name} -> {code}")
        
        if not dry_run:
            session.commit()
            print(f"Fixed {fixed} locations")
        else:
            print(f"DRY RUN: Would fix {fixed} locations")
        
        return fixed
        
    finally:
        session.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Verify and fix country codes")
    parser.add_argument("--fix", action="store_true", help="Actually fix the codes (default: dry run)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    # First, verify
    stats, invalid = verify_country_codes()
    
    # Then fix if requested
    if args.fix:
        print("\nFixing country codes...")
        fixed = fix_country_codes(dry_run=False)
        print(f"Fixed {fixed} locations")
        
        # Verify again
        print("\nVerifying after fix...")
        stats, invalid = verify_country_codes()