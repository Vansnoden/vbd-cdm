#-*- coding:utf-8 -*-

import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

project_root = Path(__file__).parent.parent
default_db_path = project_root / os.getenv("SQL_LITE_DB_NAME")

SQLITE_URL = "sqlite:///%s" % (default_db_path)
POSTGRES_URL = "postgresql://%s:%s@%s:%s/%s" % (
    os.getenv("DB_USER"),
    os.getenv("DB_PASSWORD"),
    os.getenv("DB_HOST"),
    os.getenv("DB_PORT"),
    os.getenv("DB_NAME"),
)

DATABASE_URL = os.getenv("DATABASE_URL", SQLITE_URL)

# Special handling for SQLite (needs connect_args for foreign keys)
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False  # Set to True for SQL logging
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """Dependency for FastAPI or context manager for notebooks"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()