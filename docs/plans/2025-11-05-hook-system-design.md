# Hook System Design

**Date:** 2025-11-05
**Status:** Approved

## Overview

Refactor the system to support multiple "hooks" that execute when clients are added, removed, or have their hostnames changed. This replaces the current hardcoded calls to `regenerate_hosts_file()` with a flexible hook system that allows easy addition of new integrations (e.g., Prometheus service discovery).

## Requirements

### Functional Requirements

1. **Event Triggers**: Hooks must run on these events:
   - Client registration (new client joins)
   - Client hostname changes (existing client updates hostname)
   - Client removal (pruning or explicit deletion)

2. **Hook Execution**:
   - Sequential execution in registration order
   - Continue on error (log failures but don't stop remaining hooks)
   - Each hook receives event context with config, database session, and event metadata

3. **Hook Discovery**:
   - Plugin-style architecture with decorator pattern
   - Hooks register themselves via `@register_hook` decorator
   - New hooks added by creating file and importing in `hooks/__init__.py`

4. **Hook Context**: Hooks receive:
   - Event type (which event triggered execution)
   - App configuration object
   - Database session factory
   - Event metadata (optional client details)

### Non-Functional Requirements

- Simple to add new hooks (create file, add decorator, import)
- Type-safe hook interface
- Clear error logging with hook names
- Testable (unit test hooks independently, integration test manager)

## Architecture

### Component Structure

```
hook_manager.py          # Core hook registry and execution
hooks/
  __init__.py           # Import all hooks to trigger registration
  hosts_file.py         # Regenerate /etc/hosts hook
  prometheus_sd.py      # Future: Prometheus service discovery
```

### Core Components

#### 1. HookManager (`hook_manager.py`)

Central registry and execution engine:

```python
from enum import Enum
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any
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

# Global registry
_hook_registry: list[Callable[[HookContext], None]] = []

def register_hook(func: Callable[[HookContext], None]) -> Callable[[HookContext], None]:
    """Decorator to register a hook function"""
    _hook_registry.append(func)
    logger.info(f"Registered hook: {func.__name__}")
    return func

def trigger_hooks(event_type: EventType, context: HookContext):
    """
    Execute all registered hooks for the given event.

    Runs hooks sequentially. If a hook raises an exception,
    logs the error and continues with remaining hooks.
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
```

#### 2. Hook Interface

Hooks are simple functions with this signature:

```python
def hook_function(context: HookContext) -> None:
    """
    Hook function receives context and returns None.

    Can filter on event_type if hook only cares about specific events.
    Should handle its own errors gracefully where possible.
    """
    pass
```

#### 3. Example Hook Implementation

```python
# hooks/hosts_file.py
from hook_manager import register_hook, HookContext, EventType
from models import Client
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

HOSTS_FILE_PATH = "/run/wg_fleet_hosts"

@register_hook
def regenerate_hosts_file_hook(context: HookContext):
    """Regenerate /run/wg_fleet_hosts when clients change"""

    # Filter events - only regenerate on client changes
    if context.event_type not in [
        EventType.CLIENT_ADDED,
        EventType.CLIENT_HOSTNAME_CHANGED,
        EventType.CLIENT_REMOVED
    ]:
        return

    try:
        lines = []

        with context.session_factory() as session:
            # Query all clients with hostnames
            clients = session.query(Client).filter(
                Client.hostname.isnot(None)
            ).all()

            for client in clients:
                fqdn = f"{client.hostname}.{client.fleet_id}.{context.config.domain}"
                lines.append(f"{client.assigned_ip} {fqdn}")

        # Write atomically (write to temp, then rename)
        temp_path = Path(f"{HOSTS_FILE_PATH}.tmp")
        with temp_path.open('w') as f:
            f.write('\n'.join(lines) + '\n')

        temp_path.rename(HOSTS_FILE_PATH)
        logger.info(f"Regenerated hosts file with {len(lines)} entries")

    except Exception as e:
        logger.error(f"Failed to regenerate hosts file: {e}", exc_info=True)
        raise
```

#### 4. Hook Discovery

```python
# hooks/__init__.py
"""
Import all hook modules to trigger registration.

Add new hooks by creating a module and importing it here.
"""

from . import hosts_file
# from . import prometheus_sd  # Future hook
```

### Integration Points

Replace existing `hosts.regenerate_hosts_file()` calls with `trigger_hooks()`:

#### Main.py - Startup

```python
from hook_manager import trigger_hooks, EventType, HookContext

# After setting up WireGuard interfaces
trigger_hooks(EventType.STARTUP, HookContext(
    event_type=EventType.STARTUP,
    config=app_config,
    session_factory=session_factory
))
```

#### Routes.py - Hostname Changes

```python
from hook_manager import trigger_hooks, EventType, HookContext

# After committing hostname change
if hostname_changed:
    trigger_hooks(EventType.CLIENT_HOSTNAME_CHANGED, HookContext(
        event_type=EventType.CLIENT_HOSTNAME_CHANGED,
        config=app_config,
        session_factory=_session_factory,
        client_data={
            'ip': client_ip,
            'hostname': unique_hostname,
            'fleet_id': fleet_name
        }
    ))
```

#### Pruning.py - Client Removal

```python
from hook_manager import trigger_hooks, EventType, HookContext

# After pruning clients
if prune_count > 0:
    trigger_hooks(EventType.CLIENT_REMOVED, HookContext(
        event_type=EventType.CLIENT_REMOVED,
        config=app_config,
        session_factory=session_factory,
        client_data={'count': prune_count}
    ))
```

## Migration Strategy

### Phase 1: Create Hook Infrastructure
1. Create `hook_manager.py` with EventType, HookContext, registry, and trigger_hooks()
2. Create `hooks/` package directory
3. Create `hooks/__init__.py` (empty initially)

### Phase 2: Migrate Hosts File Hook
1. Create `hooks/hosts_file.py` with `@register_hook` decorator
2. Move `regenerate_hosts_file()` logic into the hook
3. Import in `hooks/__init__.py`

### Phase 3: Update Call Sites
1. Replace `hosts.regenerate_hosts_file()` in main.py with `trigger_hooks(EventType.STARTUP, ...)`
2. Replace call in routes.py with `trigger_hooks(EventType.CLIENT_HOSTNAME_CHANGED, ...)`
3. Replace call in pruning.py with `trigger_hooks(EventType.CLIENT_REMOVED, ...)`

### Phase 4: Update Tests
1. Adapt existing `test_hosts.py` tests to test the hook directly
2. Create `test_hook_manager.py` for registry and execution tests
3. Update integration tests to use hook system

### Phase 5: Cleanup
1. Consider removing old `hosts.py` (or keep for backward compatibility)
2. Update documentation

## Testing Strategy

### Unit Tests

**Hook Manager Tests** (`test_hook_manager.py`):
- Test hook registration with decorator
- Test trigger_hooks executes all registered hooks
- Test error handling (continue on failure, log errors)
- Test HookContext creation and passing

**Individual Hook Tests** (`test_hooks.py`):
- Test hosts_file hook with mock database
- Test event filtering (hook only runs on relevant events)
- Test atomic file writing
- Mock filesystem operations for isolation

### Integration Tests

- Test full flow: client change → hook trigger → file written
- Test multiple hooks execute in order
- Test one hook failure doesn't prevent others from running

### Existing Test Migration

Current `test_hosts.py` tests can be adapted:
```python
# Old
def test_regenerate_hosts_file(test_db_with_clients):
    regenerate_hosts_file(config, session_factory, hosts_path)

# New
def test_hosts_file_hook(test_db_with_clients):
    from hooks.hosts_file import regenerate_hosts_file_hook
    context = HookContext(
        event_type=EventType.CLIENT_HOSTNAME_CHANGED,
        config=config,
        session_factory=session_factory
    )
    regenerate_hosts_file_hook(context)
```

## Future Enhancements

1. **Hook Priorities**: Add `@register_hook(priority=10)` to control execution order
2. **Async Hooks**: Support async hook functions for I/O-bound operations
3. **Hook Filtering**: Pass event filters to register_hook: `@register_hook(events=[EventType.CLIENT_ADDED])`
4. **Hook Metrics**: Track hook execution time and failure rates
5. **Conditional Hooks**: Enable/disable hooks via config file

## Example: Adding Prometheus Service Discovery

```python
# hooks/prometheus_sd.py
from hook_manager import register_hook, HookContext, EventType
from models import Client
import json
import logging

logger = logging.getLogger(__name__)

@register_hook
def prometheus_sd_hook(context: HookContext):
    """Generate Prometheus file-based service discovery config"""

    # Only regenerate on client changes
    if context.event_type not in [
        EventType.CLIENT_ADDED,
        EventType.CLIENT_HOSTNAME_CHANGED,
        EventType.CLIENT_REMOVED
    ]:
        return

    targets = []

    with context.session_factory() as session:
        clients = session.query(Client).filter(
            Client.hostname.isnot(None)
        ).all()

        for client in clients:
            targets.append({
                'targets': [f'{client.assigned_ip}:9100'],
                'labels': {
                    'job': 'node_exporter',
                    'fleet': client.fleet_id,
                    'hostname': client.hostname
                }
            })

    # Write Prometheus SD config
    sd_path = '/etc/prometheus/sd/wg_fleet.json'
    with open(sd_path, 'w') as f:
        json.dump(targets, f, indent=2)

    logger.info(f"Updated Prometheus SD config with {len(targets)} targets")

# Then import in hooks/__init__.py:
# from . import prometheus_sd
```

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Hook failure cascades | All hooks stop executing | Continue on error, log failures |
| Import order issues | Hooks not registered | Explicit imports in hooks/__init__.py |
| Hook execution too slow | Delays response times | Log timing, consider async in future |
| Testing complexity | Hard to test hooks in isolation | Direct hook function calls in tests |
| Debugging hook issues | Unclear which hook failed | Detailed logging with hook names |

## Success Criteria

- [ ] All existing hosts file functionality works identically
- [ ] All existing tests pass with hook system
- [ ] Can add new hook by creating file and adding one import line
- [ ] Hook failures are logged clearly with hook names
- [ ] One hook failure doesn't prevent others from running
- [ ] Code is simpler (fewer direct function calls, more declarative)
