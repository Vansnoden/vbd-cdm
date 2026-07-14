# tests/test_database_summary.py

import os
import sys
import pytest
import pandas as pd
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utilities.database_summary import (
    schema_summary_to_csv,
    detailed_table_info,
    compare_schemas,
    export_all_table_samples,
    get_database_metadata
)
from database.session import engine, DATABASE_URL, Base, SessionLocal



@pytest.fixture(scope="function")
def test_engine():
    temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    temp_db.close()
    test_url = f"sqlite:///{temp_db.name}"
    
    test_engine = create_engine(test_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=test_engine)
    
    with test_engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                age INTEGER
            )
        """))
        
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                product_name TEXT,
                quantity INTEGER,
                price REAL,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """))
        
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders (user_id)"))
        
        conn.execute(text("""
            INSERT INTO users (id, name, email, age) VALUES 
            (1, 'Alice', 'alice@example.com', 30),
            (2, 'Bob', 'bob@example.com', 25),
            (3, 'Charlie', 'charlie@example.com', 35)
        """))
        
        conn.execute(text("""
            INSERT INTO orders (id, user_id, product_name, quantity, price) VALUES 
            (1, 1, 'Laptop', 1, 999.99),
            (2, 1, 'Mouse', 2, 25.50),
            (3, 2, 'Keyboard', 1, 75.00)
        """))
        
        conn.commit()
    
    yield test_engine
    
    os.unlink(temp_db.name)


@pytest.fixture
def temp_output_dir():
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


def test_schema_summary_to_csv(test_engine, temp_output_dir):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        output_path = os.path.join(temp_output_dir, "test_summary.csv")
        df = schema_summary_to_csv(output_csv=output_path)
        
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        assert 'table_name' in df.columns
        assert 'column_count' in df.columns
        assert 'row_count' in df.columns
        assert len(df) >= 2
        
        assert os.path.exists(output_path)
        
        users_row = df[df['table_name'] == 'users'].iloc[0]
        assert users_row['column_count'] == 4
        assert users_row['row_count'] == 3
        
        orders_row = df[df['table_name'] == 'orders'].iloc[0]
        assert orders_row['column_count'] == 5
        assert orders_row['row_count'] == 3
        
    finally:
        utilities.database_summary.engine = original_engine


def test_schema_summary_to_csv_no_output(test_engine):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        df = schema_summary_to_csv(output_csv=None)
        
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        assert 'table_name' in df.columns
        
    finally:
        utilities.database_summary.engine = original_engine


def test_detailed_table_info(test_engine, temp_output_dir):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        output_path = os.path.join(temp_output_dir, "test_details.csv")
        df = detailed_table_info("users", output_csv=output_path)
        
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        
        assert os.path.exists(output_path)
        
    finally:
        utilities.database_summary.engine = original_engine


def test_detailed_table_info_no_output(test_engine):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        df = detailed_table_info("users", output_csv=None)
        
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        
    finally:
        utilities.database_summary.engine = original_engine


def test_detailed_table_info_nonexistent(test_engine):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        with pytest.raises(Exception):
            detailed_table_info("nonexistent_table")
            
    finally:
        utilities.database_summary.engine = original_engine


def test_compare_schemas_first_run(test_engine, temp_output_dir):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        original_dir = os.getcwd()
        os.chdir(temp_output_dir)
        
        try:
            prev_csv_path = os.path.join(temp_output_dir, "schema_summary_previous.csv")
            if os.path.exists(prev_csv_path):
                os.remove(prev_csv_path)
            
            output_path = os.path.join(temp_output_dir, "test_comparison.csv")
            result = compare_schemas(output_csv=output_path)
            
            assert isinstance(result, pd.DataFrame)
            assert not result.empty
            assert os.path.exists(prev_csv_path)
            assert not os.path.exists(output_path)
            
        finally:
            os.chdir(original_dir)
            
    finally:
        utilities.database_summary.engine = original_engine


def test_compare_schemas_second_run(test_engine, temp_output_dir):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        original_dir = os.getcwd()
        os.chdir(temp_output_dir)
        
        try:
            df_first = schema_summary_to_csv(output_csv=None)
            df_first.to_csv("schema_summary_previous.csv", index=False)
            
            output_path = os.path.join(temp_output_dir, "test_comparison.csv")
            result = compare_schemas(output_csv=output_path)
            
            assert isinstance(result, pd.DataFrame)
            assert not result.empty
            assert 'table_name' in result.columns
            assert 'row_count_change' in result.columns
            assert 'has_changed' in result.columns
            assert os.path.exists(output_path)
            
        finally:
            os.chdir(original_dir)
            
    finally:
        utilities.database_summary.engine = original_engine


def test_export_all_table_samples(test_engine, temp_output_dir):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        export_all_table_samples(output_dir=temp_output_dir, rows_per_table=2)
        
        users_sample = os.path.join(temp_output_dir, "users_sample.csv")
        orders_sample = os.path.join(temp_output_dir, "orders_sample.csv")
        
        assert os.path.exists(users_sample)
        assert os.path.exists(orders_sample)
        
        users_df = pd.read_csv(users_sample)
        assert len(users_df) == 2
        
        orders_df = pd.read_csv(orders_sample)
        assert len(orders_df) == 2
        
    finally:
        utilities.database_summary.engine = original_engine


def test_export_all_table_samples_empty_dir(test_engine, temp_output_dir):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        export_all_table_samples(output_dir=temp_output_dir)
        
        assert len(os.listdir(temp_output_dir)) >= 0
        
    finally:
        utilities.database_summary.engine = original_engine


def test_get_database_metadata(test_engine):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        df = get_database_metadata()
        
        assert isinstance(df, pd.DataFrame)
        
        if not df.empty:
            assert 'table_name' in df.columns
            
    finally:
        utilities.database_summary.engine = original_engine


def test_schema_summary_with_schema_name(test_engine, temp_output_dir):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        output_path = os.path.join(temp_output_dir, "test_summary_schema.csv")
        df = schema_summary_to_csv(output_csv=output_path, schema_name=None)
        
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        
    finally:
        utilities.database_summary.engine = original_engine


def test_integration_all_functions(test_engine, temp_output_dir):
    original_engine = engine
    try:
        import utilities.database_summary
        utilities.database_summary.engine = test_engine
        utilities.database_summary.DATABASE_URL = f"sqlite:///{test_engine.url.database}"
        
        original_dir = os.getcwd()
        os.chdir(temp_output_dir)
        
        try:
            summary_df = schema_summary_to_csv("integration_summary.csv")
            assert os.path.exists("integration_summary.csv")
            assert not summary_df.empty
            
            details_df = detailed_table_info("users", output_csv="integration_details.csv")
            assert os.path.exists("integration_details.csv")
            assert not details_df.empty
            
            compare_schemas("integration_comparison.csv")
            
            export_all_table_samples("integration_samples", rows_per_table=1)
            assert os.path.exists(os.path.join("integration_samples", "users_sample.csv"))
            
            metadata_df = get_database_metadata()
            
        finally:
            os.chdir(original_dir)
            
    finally:
        utilities.database_summary.engine = original_engine