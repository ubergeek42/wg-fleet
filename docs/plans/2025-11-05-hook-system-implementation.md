# Hook System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor hardcoded `regenerate_hosts_file()` calls into a flexible hook system that supports multiple event-triggered actions.

**Architecture:** Decorator-based registration pattern with a global registry. Hooks register themselves via `@register_hook` decorator and are triggered sequentially with continue-on-error semantics.

**Tech Stack:** Python 3, dataclasses, enum, SQLAlchemy (existing)

---

## Task 1: Create Hook Manager Infrastructure

**Files:**
- Create: `hook_manager.py`
- Create: `test_hook_manager.py`

**Step 1: Write the failing test for hook registration**

Create `test_hook_manager.py`:

```python
import pytest
from hook_manager import register_hook, HookContext, EventType, trigger_hooks, _hook_registry


def test_register_hook_decorator():
    """Test that @register_hook adds function to registry"""
    # Clear registry for test isolation
    _hook_registry.clear()

    @register_hook
    def test_hook(context: HookContext):
        pass

    assert test_hook in _hook_registry
    assert len(_hook_registry) == 1


def test_hook_receives_context():
    """Test that hook is called with context"""
    _hook_registry.clear()
    called_with = {}

    @register_hook
    def capture_context_hook(context: HookContext):
        called_with['event_type'] = context.event_type
        called_with['config'] = context.config

    config = {'domain': 'test.local'}
    context = HookContext(
        event_type=EventType.CLIENT_ADDED,
        config=config,
        session_factory=lambda: None
    )

    trigger_hooks(EventType.CLIENT_ADDED, context)

    assert called_with['event_type'] == EventType.CLIENT_ADDED
    assert called_with['config'] == config
```

**Step 2: Run test to verify it fails**

Run: `pytest test_hook_manager.py -v`
Expected: FAIL - "ModuleNotFoundError: No module named 'hook_manager'"

**Step 3: Write minimal hook_manager.py implementation**

Create `hook_manager.py`:

```python
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
```

**Step 4: Run test to verify it passes**

Run: `pytest test_hook_manager.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add hook_manager.py test_hook_manager.py
git commit -m "feat: add hook manager with decorator registration"
```

---

## Task 2: Add Error Handling Tests

**Files:**
- Modify: `test_hook_manager.py`

**Step 1: Write the failing test for error handling**

Add to `test_hook_manager.py`:

```python
def test_hook_continues_on_error():
    """Test that one hook failure doesn't stop others"""
    _hook_registry.clear()
    execution_order = []

    @register_hook
    def first_hook(context: HookContext):
        execution_order.append('first')

    @register_hook
    def failing_hook(context: HookContext):
        execution_order.append('failing')
        raise ValueError("Hook failed")

    @register_hook
    def third_hook(context: HookContext):
        execution_order.append('third')

    context = HookContext(
        event_type=EventType.CLIENT_ADDED,
        config={},
        session_factory=lambda: None
    )

    # Should not raise exception
    trigger_hooks(EventType.CLIENT_ADDED, context)

    # All hooks should execute
    assert execution_order == ['first', 'failing', 'third']


def test_hook_error_logging(caplog):
    """Test that hook errors are logged with hook name"""
    _hook_registry.clear()

    @register_hook
    def error_hook(context: HookContext):
        raise RuntimeError("Test error")

    context = HookContext(
        event_type=EventType.CLIENT_ADDED,
        config={},
        session_factory=lambda: None
    )

    with caplog.at_level(logging.ERROR):
        trigger_hooks(EventType.CLIENT_ADDED, context)

    assert "error_hook" in caplog.text
    assert "Test error" in caplog.text
```

**Step 2: Run test to verify it passes**

Run: `pytest test_hook_manager.py::test_hook_continues_on_error -v`
Run: `pytest test_hook_manager.py::test_hook_error_logging -v`
Expected: PASS (implementation already handles this)

**Step 3: Commit**

```bash
git add test_hook_manager.py
git commit -m "test: add error handling tests for hook manager"
```

---

## Task 3: Create Hooks Package Structure

**Files:**
- Create: `hooks/__init__.py`
- Create: `hooks/hosts_file.py`
- Create: `test_hooks.py`

**Step 1: Write the failing test for hosts file hook**

Create `test_hooks.py`:

```python
import pytest
from pathlib import Path
from hook_manager import HookContext, EventType
from hooks.hosts_file import regenerate_hosts_file_hook
from models import Client


def test_hosts_file_hook_generates_entries(test_db_with_clients, app_config, tmp_path):
    """Test that hosts file hook generates correct entries"""
    # Use the test_db_with_clients fixture from existing tests
    # Assuming it provides session_factory and has clients with hostnames

    # Override hosts file path for testing
    import hooks.hosts_file
    original_path = hooks.hosts_file.HOSTS_FILE_PATH
    test_hosts_path = tmp_path / "hosts"
    hooks.hosts_file.HOSTS_FILE_PATH = str(test_hosts_path)

    try:
        context = HookContext(
            event_type=EventType.CLIENT_HOSTNAME_CHANGED,
            config=app_config,
            session_factory=test_db_with_clients['session_factory']
        )

        regenerate_hosts_file_hook(context)

        # Verify hosts file was created
        assert test_hosts_path.exists()

        # Verify content
        content = test_hosts_path.read_text()
        lines = content.strip().split('\n')

        # Should have entries for clients with hostnames
        assert len(lines) > 0

        # Verify format: <ip> <hostname>.<fleet>.<domain>
        for line in lines:
            parts = line.split()
            assert len(parts) == 2
            ip, fqdn = parts
            assert '.' in ip  # IPv6 has colons, but also dots
            assert app_config.domain in fqdn

    finally:
        hooks.hosts_file.HOSTS_FILE_PATH = original_path


def test_hosts_file_hook_filters_events():
    """Test that hook only runs on relevant events"""
    # Hook should do nothing for STARTUP events
    context = HookContext(
        event_type=EventType.STARTUP,
        config={},
        session_factory=lambda: None
    )

    # Should not raise any errors or try to generate hosts
    regenerate_hosts_file_hook(context)
```

**Step 2: Run test to verify it fails**

Run: `pytest test_hooks.py -v`
Expected: FAIL - "ModuleNotFoundError: No module named 'hooks'"

**Step 3: Create hooks package and implement hosts_file hook**

Create `hooks/__init__.py`:

```python
"""
Hook modules for client lifecycle events.

Import all hook modules here to trigger registration.
"""

from . import hosts_file
```

Create `hooks/hosts_file.py`:

```python
"""
Hosts file generation hook.

Regenerates /run/wg_fleet_hosts when clients are added/changed/removed.
"""
from hook_manager import register_hook, HookContext, EventType
from models import Client
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

HOSTS_FILE_PATH = "/run/wg_fleet_hosts"


@register_hook
def regenerate_hosts_file_hook(context: HookContext):
    """
    Regenerate /run/wg_fleet_hosts from database.

    Format: <ipv6> <hostname>.<fleet>.<domain>
    Only includes clients with hostnames set.
    """
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

**Step 4: Adapt existing test fixtures**

The test relies on `test_db_with_clients` fixture from `test_hosts.py`. We need to check if this exists and is reusable.

Run: `grep -n "test_db_with_clients" test_hosts.py`

If the fixture exists, update `test_hooks.py` imports:

```python
import pytest
from pathlib import Path
from hook_manager import HookContext, EventType
from hooks.hosts_file import regenerate_hosts_file_hook
from models import Client
from test_hosts import test_db_with_clients, app_config  # Import fixtures
```

If not, copy the fixture setup from `test_hosts.py` into `conftest.py` or `test_hooks.py`.

**Step 5: Run test to verify it passes**

Run: `pytest test_hooks.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add hooks/__init__.py hooks/hosts_file.py test_hooks.py
git commit -m "feat: add hosts file hook with tests"
```

---

## Task 4: Update main.py to Use Hook System

**Files:**
- Modify: `main.py:122`

**Step 1: Write the failing integration test**

Add to `test_integration.py` (if it exists) or create new test in `test_hooks.py`:

```python
def test_startup_triggers_hooks(mocker):
    """Test that startup triggers hooks"""
    mock_trigger = mocker.patch('main.trigger_hooks')

    # This would require refactoring main.py startup logic
    # For now, we'll manually test after implementation
    pass
```

**Step 2: Update main.py to import and use hook system**

In `main.py`, find line 122:

```python
# OLD:
hosts.regenerate_hosts_file(app_config, session_factory)

# NEW:
from hook_manager import trigger_hooks, EventType, HookContext
import hooks  # Import to register hooks

# Replace line 122 with:
trigger_hooks(EventType.STARTUP, HookContext(
    event_type=EventType.STARTUP,
    config=app_config,
    session_factory=session_factory
))
```

Also remove the old import at the top of `main.py`:
```python
# Remove or comment out:
# import hosts
```

**Step 3: Run the application to verify startup works**

Run: `python main.py` (or however the app is started)
Expected: App starts successfully, hosts file is generated

Check logs for: "Registered hook: regenerate_hosts_file_hook"
Check logs for: "Regenerated hosts file with N entries"

**Step 4: Commit**

```bash
git add main.py
git commit -m "refactor: use hook system for startup hosts generation"
```

---

## Task 5: Update routes.py to Use Hook System

**Files:**
- Modify: `routes.py:217-219`

**Step 1: Update routes.py to use hook system**

In `routes.py`, find lines 217-219:

```python
# OLD:
if hostname_changed:
    hosts.regenerate_hosts_file(app_config, _session_factory)

# NEW:
from hook_manager import trigger_hooks, EventType, HookContext
import hooks  # Import to register hooks

# Replace lines 217-219 with:
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

Also remove the old import:
```python
# Remove or comment out:
# import hosts
```

**Step 2: Update existing mock tests**

Find test in `test_routes.py` that mocks `hosts.regenerate_hosts_file`:

```python
# OLD:
mock_hosts.regenerate_hosts_file.assert_called_once()

# NEW:
# Mock trigger_hooks instead
mock_trigger = mocker.patch('routes.trigger_hooks')
# ... run test ...
mock_trigger.assert_called_once()
# Optionally verify it was called with correct event type:
assert mock_trigger.call_args[0][0] == EventType.CLIENT_HOSTNAME_CHANGED
```

**Step 3: Run tests**

Run: `pytest test_routes.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add routes.py test_routes.py
git commit -m "refactor: use hook system for hostname change events"
```

---

## Task 6: Update pruning.py to Use Hook System

**Files:**
- Modify: `pruning.py:69-71`
- Modify: `test_pruning.py`

**Step 1: Update pruning.py to use hook system**

In `pruning.py`, find lines 69-71:

```python
# OLD:
if prune_count > 0:
    hosts.regenerate_hosts_file(config, session_factory)

# NEW:
from hook_manager import trigger_hooks, EventType, HookContext
import hooks  # Import to register hooks

# Replace lines 69-71 with:
if prune_count > 0:
    trigger_hooks(EventType.CLIENT_REMOVED, HookContext(
        event_type=EventType.CLIENT_REMOVED,
        config=config,
        session_factory=session_factory,
        client_data={'count': prune_count}
    ))
```

Remove old import:
```python
# Remove or comment out:
# import hosts
```

**Step 2: Update pruning tests**

In `test_pruning.py`, find mocks for `hosts.regenerate_hosts_file`:

```python
# OLD:
mock_hosts.regenerate_hosts_file.assert_called_once()

# NEW:
mock_trigger = mocker.patch('pruning.trigger_hooks')
# ... run test ...
mock_trigger.assert_called_once()
assert mock_trigger.call_args[0][0] == EventType.CLIENT_REMOVED
```

**Step 3: Run tests**

Run: `pytest test_pruning.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add pruning.py test_pruning.py
git commit -m "refactor: use hook system for client removal events"
```

---

## Task 7: Update Remaining Test Mocks

**Files:**
- Modify: `test_integration.py:70` (if applicable)
- Modify any other files that mock `hosts.regenerate_hosts_file`

**Step 1: Search for remaining references**

Run: `grep -r "mock_hosts.regenerate_hosts_file" .`

Update each mock to use `trigger_hooks` instead.

**Step 2: Run full test suite**

Run: `pytest -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add test_*.py
git commit -m "test: update all test mocks to use hook system"
```

---

## Task 8: Deprecate or Remove hosts.py

**Files:**
- Review: `hosts.py`
- Review: `test_hosts.py`

**Step 1: Check if hosts.py is still imported anywhere**

Run: `grep -r "from hosts import\|import hosts" --include="*.py" .`

Expected: No results (all imports already removed)

**Step 2: Decision point**

Options:
1. **Delete**: Remove `hosts.py` and `test_hosts.py` entirely
2. **Deprecate**: Keep with deprecation warning for backward compatibility
3. **Keep**: Keep as-is for reference

Recommended: Delete since functionality is now in `hooks/hosts_file.py`

**Step 3: If deleting, remove files**

```bash
git rm hosts.py test_hosts.py
git commit -m "refactor: remove deprecated hosts.py (moved to hooks/hosts_file.py)"
```

**Step 4: If keeping, add deprecation notice**

Add to top of `hosts.py`:

```python
"""
DEPRECATED: This module is deprecated and will be removed in a future version.

Use the hook system instead:
- Hook implementation: hooks/hosts_file.py
- Trigger hooks with: hook_manager.trigger_hooks(EventType.*, HookContext(...))
"""
import warnings
warnings.warn(
    "hosts.py is deprecated. Use hooks/hosts_file.py and hook_manager instead.",
    DeprecationWarning,
    stacklevel=2
)
```

**Step 5: Commit**

```bash
git add hosts.py
git commit -m "docs: add deprecation warning to hosts.py"
```

---

## Task 9: Update Documentation

**Files:**
- Create or modify: `README.md` (section on hooks)
- Modify: `docs/plans/2025-10-19-wg-fleet-design.md` (update references to hosts.py)

**Step 1: Add hook system documentation to README**

Add section to `README.md`:

```markdown
## Hook System

The application uses a hook system for event-driven actions when clients change.

### Adding a New Hook

1. Create a new file in `hooks/` directory (e.g., `hooks/my_hook.py`)
2. Implement your hook function:

```python
from hook_manager import register_hook, HookContext, EventType
import logging

logger = logging.getLogger(__name__)

@register_hook
def my_hook(context: HookContext):
    """Description of what this hook does"""

    # Filter events if needed
    if context.event_type != EventType.CLIENT_ADDED:
        return

    # Your hook logic here
    # Access context.config, context.session_factory, context.client_data
    logger.info("My hook executed")
```

3. Import your hook in `hooks/__init__.py`:

```python
from . import my_hook
```

4. Done! Your hook will automatically run on client events.

### Available Events

- `EventType.STARTUP`: Application startup
- `EventType.CLIENT_ADDED`: New client registered
- `EventType.CLIENT_HOSTNAME_CHANGED`: Client hostname updated
- `EventType.CLIENT_REMOVED`: Client pruned or removed

### Existing Hooks

- `hooks/hosts_file.py`: Generates `/run/wg_fleet_hosts` for hostname resolution
```

**Step 2: Update design document references**

In `docs/plans/2025-10-19-wg-fleet-design.md`, find references to `hosts.regenerate_hosts_file()` and update them:

```markdown
# OLD:
- Call `hosts.regenerate_hosts_file()`

# NEW:
- Call `hook_manager.trigger_hooks(EventType.*, HookContext(...))`
- See `docs/plans/2025-11-05-hook-system-design.md` for details
```

**Step 3: Commit**

```bash
git add README.md docs/plans/2025-10-19-wg-fleet-design.md
git commit -m "docs: add hook system documentation"
```

---

## Task 10: Final Verification

**Step 1: Run full test suite**

Run: `pytest -v --cov=. --cov-report=term-missing`
Expected: All tests pass, coverage maintained or improved

**Step 2: Manual smoke test**

```bash
# Start the application
python main.py

# Check logs for hook registration:
# "Registered hook: regenerate_hosts_file_hook"

# Verify hosts file exists:
ls -la /run/wg_fleet_hosts

# Trigger a client ping to test hostname change hook
# (use existing test client or API call)

# Check logs for hook execution:
# "Executing hook: regenerate_hosts_file_hook"
# "Regenerated hosts file with N entries"
```

**Step 3: Verify no regressions**

Check that existing functionality still works:
- Hosts file is generated on startup
- Hosts file updates when clients ping with new hostnames
- Hosts file updates when clients are pruned

**Step 4: Final commit and summary**

```bash
git log --oneline | head -n 10
```

Expected commits:
1. feat: add hook manager with decorator registration
2. test: add error handling tests for hook manager
3. feat: add hosts file hook with tests
4. refactor: use hook system for startup hosts generation
5. refactor: use hook system for hostname change events
6. refactor: use hook system for client removal events
7. test: update all test mocks to use hook system
8. refactor: remove deprecated hosts.py
9. docs: add hook system documentation

---

## Success Criteria

- [ ] All existing tests pass
- [ ] Hosts file generation works identically to before
- [ ] New hooks can be added with ~10 lines of code + import
- [ ] Hook failures don't crash the application
- [ ] Hook failures are logged with hook name
- [ ] Documentation explains how to add new hooks
- [ ] Code is DRY (hook logic centralized, not repeated)
- [ ] Tests verify hook registration, execution, and error handling

## Skills to Reference

- @superpowers:test-driven-development - Follow TDD workflow for each component
- @superpowers:systematic-debugging - If tests fail unexpectedly
- @superpowers:verification-before-completion - Before marking tasks complete
