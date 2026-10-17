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
