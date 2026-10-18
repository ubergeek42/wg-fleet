import subprocess
import logging
from typing import Optional, List, Union

logger = logging.getLogger(__name__)

class CommandError(Exception):
    """Raised when a subprocess command fails"""
    pass

def run_command(
    args: List[str],
    input_data: Optional[Union[str, bytes]] = None,
    timeout: float = 10
) -> str:
    """
    Execute command with error handling and logging.

    Args:
        args: Command and arguments as list
        input_data: Optional stdin data (not logged; use this for secrets such as private keys).
            bytes are passed through unchanged, e.g. a file whose signature is checked.
        timeout: Seconds before the command is killed. Commands run on the event loop,
            so a hung command would otherwise freeze the whole server.

    Returns:
        stdout as string

    Raises:
        CommandError: On non-zero exit code or timeout
    """
    logger.debug(f"Running command: {' '.join(args)}")

    binary = isinstance(input_data, bytes)
    try:
        result = subprocess.run(
            args,
            input=input_data,
            capture_output=True,
            text=not binary,
            check=True,
            timeout=timeout
        )
        stdout = result.stdout.decode(errors='replace') if binary else result.stdout
        return stdout.strip()
    except subprocess.TimeoutExpired as e:
        logger.error(f"Command timed out after {timeout}s: {' '.join(args)}")
        raise CommandError(f"Command timed out after {timeout}s") from e
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {' '.join(args)}")
        logger.error(f"Exit code: {e.returncode}")
        stderr = e.stderr.decode(errors='replace') if binary else e.stderr
        logger.error(f"Stderr: {stderr}")
        raise CommandError(f"Command failed: {stderr}") from e
