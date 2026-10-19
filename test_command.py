import pytest
from command import run_command, CommandError

def test_run_command_success():
    """Test successful command execution"""
    result = run_command(['echo', 'hello'])
    assert result == 'hello'

def test_run_command_with_input():
    """Test command with stdin"""
    result = run_command(['cat'], input_data='test input')
    assert result == 'test input'

def test_run_command_failure():
    """Test that failed commands raise CommandError"""
    with pytest.raises(CommandError):
        run_command(['false'])

def test_run_command_timeout():
    """Test that a hung command is killed and raises CommandError"""
    import time
    start = time.monotonic()
    with pytest.raises(CommandError, match="timed out"):
        run_command(['sleep', '5'], timeout=0.2)
    assert time.monotonic() - start < 2

def test_run_command_quiet_failure(caplog):
    """Test an expected failure still raises, but isn't logged as an error"""
    import logging
    with caplog.at_level(logging.DEBUG, logger='command'):
        with pytest.raises(CommandError):
            run_command(['sh', '-c', 'echo "No such device" >&2; exit 1'], quiet=True)
    assert 'No such device' in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

def test_run_command_failure_logged_as_error(caplog):
    """Test unexpected failures are still logged at error"""
    import logging
    with pytest.raises(CommandError):
        run_command(['false'])
    assert [r for r in caplog.records if r.levelno == logging.ERROR]
