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

def test_init_db_upgrades_old_database(tmp_path):
    """Test a database from before site/host_key keeps its clients and gains the new columns"""
    import sqlite3
    db_path = tmp_path / 'clients.db'
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fleet_id VARCHAR NOT NULL, public_key VARCHAR NOT NULL,
        assigned_ip VARCHAR NOT NULL, http_request_ip VARCHAR NOT NULL, hostname VARCHAR,
        timestamp DATETIME NOT NULL)""")
    conn.execute("""INSERT INTO clients (fleet_id, public_key, assigned_ip, http_request_ip, hostname, timestamp)
                    VALUES ('southeast', 'pk', 'fd00::5', '1.2.3.4', 't101', '2026-10-18 12:00:00')""")
    conn.commit()
    conn.close()

    engine = init_db(str(db_path))
    # Running it again is a no-op
    init_db(str(db_path))

    with get_session_factory(engine)() as session:
        client = session.query(Client).one()
        assert (client.hostname, client.site, client.host_key) == ('t101', None, None)
        client.site = 'BigSouth2026/MidAtlantic/VirginiaTech'
        session.commit()
