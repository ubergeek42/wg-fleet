import pytest
import logging
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
