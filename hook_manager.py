"""
Hook system for triggering actions on client lifecycle events.

Provides decorator-based registration and sequential execution with error tolerance.
"""
from enum import Enum
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Events that trigger hooks"""
    STARTUP = "startup"
    CLIENT_ADDED = "client.added"
    CLIENT_HOSTNAME_CHANGED = "client.hostname_changed"
    CLIENT_REMOVED = "client.removed"


@dataclass
class HookContext:
    """Context passed to all hooks"""
    event_type: EventType
    config: Any  # App config object
    session_factory: Callable
    client_data: Optional[Dict[str, Any]] = None


# Global registry of hook functions
_hook_registry: List[Callable[[HookContext], None]] = []


def register_hook(func: Callable[[HookContext], None]) -> Callable[[HookContext], None]:
    """
    Decorator to register a hook function.

    Usage:
        @register_hook
        def my_hook(context: HookContext):
            # Hook implementation
            pass
    """
    _hook_registry.append(func)
    logger.info(f"Registered hook: {func.__name__}")
    return func


def trigger_hooks(event_type: EventType, context: HookContext):
    """
    Execute all registered hooks for the given event.

    Runs hooks sequentially. If a hook raises an exception,
    logs the error and continues with remaining hooks.

    Args:
        event_type: The event that triggered hook execution
        context: Context object with config, session factory, and metadata
    """
    errors = []

    for hook_func in _hook_registry:
        try:
            logger.debug(f"Executing hook: {hook_func.__name__}")
            hook_func(context)
        except Exception as e:
            logger.error(
                f"Hook {hook_func.__name__} failed: {e}",
                exc_info=True
            )
            errors.append((hook_func.__name__, e))

    if errors:
        logger.warning(
            f"Hook execution completed with {len(errors)} error(s): "
            f"{[name for name, _ in errors]}"
        )
