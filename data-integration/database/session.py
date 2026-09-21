import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

project_root = Path(__file__).parent.parent

def _sqlite_url() -> str:
    name = os.getenv("SQL_LITE_DB_NAME", "vbd_cdm.db")
    path = Path(name)
    if not path.is_absolute():
        path = project_root / path
    return f"sqlite:///{path}"

def _postgres_url() -> str | None:
    user = os.getenv("DB_USER")
    pwd  = os.getenv("DB_PASSWORD")
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT")
    name = os.getenv("DB_NAME")
    if not all([user, pwd, host, port, name]):
        return None
    return f"postgresql://{user}:{pwd}@{host}:{port}/{name}"

DATABASE_URL = os.getenv("DATABASE_URL") or _sqlite_url()

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL, 
    connect_args=connect_args, 
    echo=False,
    # isolation_level=None,
    )

SessionLocal = sessionmaker(
    autocommit=False, 
    autoflush=False, 
    bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()