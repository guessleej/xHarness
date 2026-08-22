"""The plugin kernel: a shared context of services, events, and middleware.

Every registration returns a disposer. Disposers created while a plugin is
being applied are collected into that plugin's scope, so unloading a plugin
unwinds everything it contributed.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

Disposer = Callable[[], None]
Middleware = Callable[[Any, Callable[[Any], Any]], Any]


class Context:
    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._listeners: dict[str, list[Callable[[Any], Any]]] = {}
        self._middlewares: dict[str, list[Middleware]] = {}
        #: Disposers collected for the plugin currently being applied.
        self.scope: list[Disposer] | None = None

    def on_dispose(self, dispose: Disposer) -> Disposer:
        """Track a disposer in the current plugin scope (if any) and return it."""
        if self.scope is not None:
            self.scope.append(dispose)
        return dispose

    def provide(self, key: str, value: Any) -> Disposer:
        """Register a named service. Duplicate keys raise."""
        if key in self._services:
            raise KeyError(f"service already provided: {key}")
        self._services[key] = value
        return self.on_dispose(lambda: self._services.pop(key, None))

    def get(self, key: str) -> Any:
        """Get a required service. Missing keys raise."""
        if key not in self._services:
            raise KeyError(f"service not found: {key}")
        return self._services[key]

    def optional(self, key: str, default: Any = None) -> Any:
        return self._services.get(key, default)

    def on(self, event: str, handler: Callable[[Any], Any]) -> Disposer:
        """Subscribe to an event. Handler failures are contained and logged."""
        handlers = self._listeners.setdefault(event, [])
        handlers.append(handler)

        def dispose() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return self.on_dispose(dispose)

    def emit(self, event: str, payload: Any = None) -> None:
        for handler in list(self._listeners.get(event, [])):
            try:
                handler(payload)
            except Exception as error:  # noqa: BLE001 - listeners must not break the host
                print(f"[xharness] listener for {event!r} failed: {error}", file=sys.stderr)

    def intercept(self, event: str, middleware: Middleware) -> Disposer:
        """Add a middleware around an interceptable call (e.g. "llm/stream")."""
        chain = self._middlewares.setdefault(event, [])
        chain.append(middleware)

        def dispose() -> None:
            if middleware in chain:
                chain.remove(middleware)

        return self.on_dispose(dispose)

    def invoke(self, event: str, payload: Any, terminal: Callable[[Any], Any]) -> Any:
        """Run an interceptable call through its middleware chain to a terminal."""
        chain = list(self._middlewares.get(event, []))

        def step(index: int, current: Any) -> Any:
            if index >= len(chain):
                return terminal(current)
            return chain[index](current, lambda nxt: step(index + 1, nxt))

        return step(0, payload)


class Plugin:
    """A plugin contributes services, events, and tools to the shared context."""

    def __init__(self, name: str, apply: Callable[[Context, Any], None]) -> None:
        self.name = name
        self._apply = apply

    def apply(self, ctx: Context, config: Any) -> None:
        self._apply(ctx, config)


class Harness:
    """The plugin host. Applies plugins in order; unloading unwinds a plugin's scope."""

    def __init__(self) -> None:
        self.ctx = Context()
        self._scopes: dict[str, list[Disposer]] = {}

    def use(self, plugin_id: str, plugin: Plugin, config: Any = None, disabled: bool = False) -> None:
        if disabled:
            return
        if plugin_id in self._scopes:
            raise ValueError(f"duplicate plugin id: {plugin_id}")
        scope: list[Disposer] = []
        previous, self.ctx.scope = self.ctx.scope, scope
        try:
            plugin.apply(self.ctx, config or {})
            self._scopes[plugin_id] = scope
        except Exception:
            for dispose in reversed(scope):
                dispose()
            raise
        finally:
            self.ctx.scope = previous
        self.ctx.emit("plugin/loaded", {"id": plugin_id, "name": plugin.name})

    def unload(self, plugin_id: str) -> None:
        scope = self._scopes.pop(plugin_id, None)
        if scope is None:
            return
        for dispose in reversed(scope):
            dispose()
        self.ctx.emit("plugin/unloaded", {"id": plugin_id})

    def dispose(self) -> None:
        for plugin_id in list(reversed(list(self._scopes))):
            self.unload(plugin_id)
