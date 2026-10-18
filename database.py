from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from models import Base
import logging

logger = logging.getLogger(__name__)

def add_missing_columns(engine):
    """
    Add columns that models have but existing tables lack.

    create_all() creates missing tables but never alters existing ones, so a
    database from an older wg-fleet would lack newer columns. New columns are
    always nullable, so adding them is enough; no data needs converting.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {column['name'] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable:
                    raise RuntimeError(f"Can't add non-nullable column {table.name}.{column.name}")
                column_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}'))
                logger.info(f"Added column {table.name}.{column.name}")

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
    add_missing_columns(engine)
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
