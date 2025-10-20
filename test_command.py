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

def test_run_command_redacts_sensitive():
    """Test that sensitive patterns are redacted (check via logging)"""
    # This test verifies the function doesn't crash with sensitive patterns
    # Actual log redaction would need log capture to verify
    result = run_command(['echo', 'secret123'], sensitive_patterns=['secret123'])
    assert result == 'secret123'  # Output is not redacted, only logs
