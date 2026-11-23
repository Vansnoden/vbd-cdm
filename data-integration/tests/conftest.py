import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set before database.session is imported by any test module: it resolves the
# URL once, at import time, so a later change would not reach the engine.
_TMP = Path(tempfile.mkdtemp(prefix="vbd-cdm-tests-"))
os.environ.pop("DATABASE_URL", None)
for _key in ("DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DB_NAME"):
    os.environ.pop(_key, None)
os.environ["SQL_LITE_DB_NAME"] = str(_TMP / "fixture.db")


@pytest.fixture(scope="session")
def engine():
    from database.session import engine as _engine

    return _engine


@pytest.fixture()
def session(engine):
    from database import models  # noqa: F401
    from database.session import Base, SessionLocal

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    bound = SessionLocal()
    try:
        yield bound
    finally:
        bound.close()