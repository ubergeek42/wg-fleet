import pytest
from unittest.mock import patch, MagicMock, call
from models import Client, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime, UTC
import tempfile
import os

@pytest.fixture
def test_db():
    """Create test database"""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    engine = create_engine(f'sqlite:///{db_path}')
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    yield session_factory, engine, db_path

    os.unlink(db_path)

def test_main_initializes_once_and_runs_pruning(test_db, tmp_path):
    """Test main() initializes fleets once and the app lifespan runs the pruner"""
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    import main

    session_factory, engine, db_path = test_db
    config_path = tmp_path / 'config.yaml'
    config_path.write_text("""
domain: test.internal
prune_timeout: 30m
fleets:
  testfleet:
    ip6: fd00::1
    subnet: fd00::/64
    external_ip: 1.2.3.4
    port: 51820
""")

    with patch('sys.argv', ['main.py', '--config', str(config_path)]), \
         patch('main.init_db', return_value=engine) as mock_init_db, \
         patch('main.uvicorn') as mock_uvicorn, \
         patch('main.initialize_fleets') as mock_initialize, \
         patch('main.prune_stale_clients', new_callable=AsyncMock) as mock_prune:
        main.main()

        mock_init_db.assert_called_once()
        mock_initialize.assert_called_once()

        app = mock_uvicorn.run.call_args[0][0]
        with TestClient(app) as client:
            assert client.get('/').status_code == 200
        mock_prune.assert_awaited_once()
        assert mock_prune.call_args[0][1] is mock_initialize.call_args[0][1]

def test_main_exits_on_startup_failure(tmp_path):
    """Test main() exits non-zero when startup fails"""
    import main

    with patch('sys.argv', ['main.py', '--config', str(tmp_path / 'missing.yaml')]), \
         patch('main.uvicorn') as mock_uvicorn:
        with pytest.raises(SystemExit) as exc_info:
            main.main()

    assert exc_info.value.code == 1
    mock_uvicorn.run.assert_not_called()
