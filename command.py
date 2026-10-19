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
    timeout: float = 10,
    quiet: bool = False
) -> str:
    """
    Execute command with error handling and logging.

    Args:
        args: Command and arguments as list
        input_data: Optional stdin data (not logged; use this for secrets such as private keys).
            bytes are passed through unchanged, e.g. a file whose signature is checked.
        timeout: Seconds before the command is killed. Commands run on the event loop,
            so a hung command would otherwise freeze the whole server.
        quiet: Log a failure at debug instead of error, for commands whose failure is an
            expected answer (e.g. checking whether an interface exists)

    Returns:
        stdout as string

    Raises:
        CommandError: On non-zero exit code or timeout
    """
    logger.debug(f"Running command: {' '.join(args)}")

    binary = isinstance(input_data, bytes)
    log = logger.debug if quiet else logger.error
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
        log(f"Command timed out after {timeout}s: {' '.join(args)}")
        raise CommandError(f"Command timed out after {timeout}s") from e
    except subprocess.CalledProcessError as e:
        log(f"Command failed: {' '.join(args)}")
        log(f"Exit code: {e.returncode}")
        stderr = e.stderr.decode(errors='replace') if binary else e.stderr
        log(f"Stderr: {stderr}")
        raise CommandError(f"Command failed: {stderr}") from e
