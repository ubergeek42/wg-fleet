import subprocess
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

class CommandError(Exception):
    """Raised when a subprocess command fails"""
    pass

def run_command(
    args: List[str],
    sensitive_patterns: Optional[List[str]] = None,
    input_data: Optional[str] = None
) -> str:
    """
    Execute command with error handling and logging.

    Args:
        args: Command and arguments as list
        sensitive_patterns: Strings to redact in logs (e.g., private keys)
        input_data: Optional stdin data

    Returns:
        stdout as string

    Raises:
        CommandError: On non-zero exit code
    """
    # Create sanitized version for logging
    log_args = args.copy()
    if sensitive_patterns:
        for i, arg in enumerate(log_args):
            for pattern in sensitive_patterns:
                if pattern in arg:
                    log_args[i] = "[REDACTED]"

    logger.info(f"Running command: {' '.join(log_args)}")

    try:
        result = subprocess.run(
            args,
            input=input_data,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {' '.join(log_args)}")
        logger.error(f"Exit code: {e.returncode}")
        logger.error(f"Stderr: {e.stderr}")
        raise CommandError(f"Command failed: {e.stderr}") from e
