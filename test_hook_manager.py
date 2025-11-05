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
