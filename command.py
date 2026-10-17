import subprocess
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

class CommandError(Exception):
    """Raised when a subprocess command fails"""
    pass

def run_command(
    args: List[str],
    input_data: Optional[str] = None
) -> str:
    """
    Execute command with error handling and logging.

    Args:
        args: Command and arguments as list
        input_data: Optional stdin data (not logged; use this for secrets such as private keys)

    Returns:
        stdout as string

    Raises:
        CommandError: On non-zero exit code
    """
    logger.debug(f"Running command: {' '.join(args)}")

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
        logger.error(f"Command failed: {' '.join(args)}")
        logger.error(f"Exit code: {e.returncode}")
        logger.error(f"Stderr: {e.stderr}")
        raise CommandError(f"Command failed: {e.stderr}") from e
