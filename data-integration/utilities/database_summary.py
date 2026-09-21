#-*- coding:utf-8 -*-

import os
import pandas as pd
from sqlalchemy import inspect, text
from database.session import engine, DATABASE_URL


def schema_summary_to_csv(output_csv: str = "outputs/data/schema_summary.csv", schema_name: str = None) -> pd.DataFrame:
    inspector = inspect(engine)
    if schema_name:
        tables = inspector.get_table_names(schema=schema_name)
    else:
        tables = inspector.get_table_names()
    summary = []
    with engine.connect() as conn:
        for table in tables:
            table_info = {
                'table_name': table,
                'schema': schema_name or 'public',
                'column_count': len(inspector.get_columns(table, schema=schema_name)),
                'index_count': len(inspector.get_indexes(table, schema=schema_name)),
                'foreign_key_count': len(inspector.get_foreign_keys(table, schema=schema_name)),
            }
            try:
                if schema_name:
                    result = conn.execute(text(f'SELECT COUNT(*) FROM {schema_name}.{table}')).scalar()
                else:
                    if DATABASE_URL.startswith("sqlite"):
                        result = conn.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar()
                    else:
                        result = conn.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar()
                table_info['row_count'] = result
            except Exception as e:
                table_info['row_count'] = None
                table_info['row_count_error'] = str(e)
            if DATABASE_URL.startswith("postgresql"):
                try:
                    size_result = conn.execute(text(f"""
                        SELECT pg_size_pretty(pg_total_relation_size('{table}')) as total_size
                    """)).scalar()
                    table_info['total_size'] = size_result
                except:
                    table_info['total_size'] = None
            elif DATABASE_URL.startswith("mysql"):
                try:
                    size_result = conn.execute(text(f"""
                        SELECT ROUND((DATA_LENGTH + INDEX_LENGTH) / 1024 / 1024, 2) as total_size_mb
                        FROM information_schema.TABLES
                        WHERE TABLE_NAME = '{table}'
                    """)).scalar()
                    table_info['total_size_mb'] = size_result
                except:
                    table_info['total_size_mb'] = None
            summary.append(table_info)
    df = pd.DataFrame(summary)
    if output_csv:
        df.to_csv(output_csv, index=False)
        print(f"Schema summary exported to {output_csv}")
    return df


def detailed_table_info(table_name: str, schema_name: str = None, output_csv: str = None) -> pd.DataFrame:
    inspector = inspect(engine)
    columns = inspector.get_columns(table_name, schema=schema_name)
    df = pd.DataFrame(columns)
    if 'type' in df.columns:
        df['data_type'] = df['type'].astype(str)
        df = df.drop('type', axis=1)
    if output_csv:
        df.to_csv(output_csv, index=False)
        print(f"Detailed info for table '{table_name}' exported to {output_csv}")
    return df


def compare_schemas(output_csv: str = "schema_comparison.csv") -> pd.DataFrame:
    current_summary = schema_summary_to_csv(output_csv=None)
    prev_csv = "schema_summary_previous.csv"
    if os.path.exists(prev_csv):
        previous_summary = pd.read_csv(prev_csv)
        comparison = current_summary.merge(
            previous_summary, 
            on='table_name', 
            suffixes=('_current', '_previous')
        )
        comparison['row_count_change'] = comparison['row_count_current'] - comparison['row_count_previous']
        comparison['has_changed'] = (
            (comparison['row_count_change'] != 0) |
            (comparison['column_count_current'] != comparison['column_count_previous'])
        )
        comparison.to_csv(output_csv, index=False)
        print(f"Schema comparison exported to {output_csv}")
        return comparison
    current_summary.to_csv(prev_csv, index=False)
    print(f"No previous schema found. Saved current as baseline in {prev_csv}")
    return current_summary


def export_all_table_samples(output_dir: str = "table_samples", rows_per_table: int = 100):
    os.makedirs(output_dir, exist_ok=True)
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    with engine.connect() as conn:
        for table in tables:
            try:
                df = pd.read_sql(text(f'SELECT * FROM {table} LIMIT {rows_per_table}'), conn)
                output_path = os.path.join(output_dir, f"{table}_sample.csv")
                df.to_csv(output_path, index=False)
                print(f"Exported {len(df)} rows from '{table}' to {output_path}")
            except Exception as e:
                print(f"Error exporting {table}: {e}")


def get_database_metadata() -> pd.DataFrame:
    inspector = inspect(engine)    
    metadata = []
    with engine.connect() as conn:
        for table in inspector.get_table_names():
            pk = inspector.get_pk_constraint(table)
            fks = inspector.get_foreign_keys(table)
            indexes = inspector.get_indexes(table)
            metadata.append({
                'table_name': table,
                'primary_key': pk.get('constrained_columns', []),
                'foreign_keys': [(fk['constrained_columns'], fk['referred_table'], fk['referred_columns']) 
                                for fk in fks],
                'indexes': [idx['column_names'] for idx in indexes],
                'unique_constraints': [idx['column_names'] for idx in indexes if idx.get('unique', False)]
            })
    return pd.DataFrame(metadata)
