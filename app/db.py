"""Database engine and session setup."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

DEFAULT_DB_URL = "sqlite:///drugshortage.db"


def get_engine(url: str | None = None):
    url = url or os.environ.get("DATABASE_URL", DEFAULT_DB_URL)
    return create_engine(url)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
