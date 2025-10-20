import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from database import init_db, get_session_factory
from models import Base, Client
from datetime import datetime, UTC
import tempfile
import os

@pytest.fixture
def test_db():
    """Create temporary test database"""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    engine = create_engine(f'sqlite:///{db_path}')
    Base.metadata.create_all(engine)

    yield engine, db_path

    os.unlink(db_path)

def test_init_db_creates_tables(test_db):
    """Test that init_db creates the clients table"""
    engine, db_path = test_db

    # Verify table exists by querying schema
    with engine.connect() as conn:
        result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='clients'"))
        assert result.fetchone() is not None

def test_client_model_creation(test_db):
    """Test creating a Client record"""
    engine, db_path = test_db
    session_factory = get_session_factory(engine)

    with session_factory() as session:
        client = Client(
            fleet_id="testfleet",
            public_key="test_pubkey_123",
            assigned_ip="fd00::100",
            http_request_ip="192.168.1.1",
            hostname="testhost",
            timestamp=datetime.now(UTC)
        )
        session.add(client)
        session.commit()

        # Query it back
        retrieved = session.query(Client).filter_by(public_key="test_pubkey_123").first()
        assert retrieved is not None
        assert retrieved.fleet_id == "testfleet"
        assert retrieved.hostname == "testhost"
