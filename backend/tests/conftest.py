import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import SessionLocal, engine as application_engine
from app import models  # Register every mapped table in Base.metadata.


@pytest.fixture(scope="session", autouse=True)
def isolated_postgres_schema() -> Iterator[None]:
    database_url = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url
    schema_name = f"pytest_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    test_engine: Engine | None = None
    schema_created = False

    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        schema_created = True

        test_engine = create_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema_name}"},
            pool_pre_ping=True,
        )
        SessionLocal.configure(bind=test_engine)
        Base.metadata.create_all(test_engine)
        yield
    finally:
        SessionLocal.configure(bind=application_engine)
        if test_engine is not None:
            test_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        admin_engine.dispose()
