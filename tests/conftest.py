"""Shared pytest fixtures for bird-manager tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, ensure_schema


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    ensure_schema(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def parser():
    from app.parsing_engine import ParsingEngine

    return ParsingEngine()
