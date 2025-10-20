from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from models import Base
import logging

logger = logging.getLogger(__name__)

def init_db(db_path: str = "/var/lib/wg-fleet/clients.db"):
    """
    Initialize database and create tables.

    Args:
        db_path: Path to SQLite database file

    Returns:
        SQLAlchemy engine
    """
    engine = create_engine(f'sqlite:///{db_path}')
    Base.metadata.create_all(engine)
    logger.info(f"Database initialized at {db_path}")
    return engine

def get_session_factory(engine):
    """
    Create a session factory for the given engine.

    Args:
        engine: SQLAlchemy engine

    Returns:
        Session factory (sessionmaker)
    """
    return sessionmaker(bind=engine)
