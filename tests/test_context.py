import pytest

from xharness.context import Context, Harness, Plugin


def test_provide_get_roundtrip_and_duplicates():
    ctx = Context()
    ctx.provide("a", 1)
    assert ctx.get("a") == 1
    with pytest.raises(KeyError):
        ctx.provide("a", 2)
    with pytest.raises(KeyError):
        ctx.get("missing")
    assert ctx.optional("missing") is None


def test_unload_unwinds_services_and_listeners():
    harness = Harness()
    calls = []

    def apply(ctx, _config):
        ctx.provide("svc", "value")
        ctx.on("ping", lambda _payload: calls.append(1))

    harness.use("p1", Plugin("p1", apply))
    assert harness.ctx.get("svc") == "value"
    harness.ctx.emit("ping")
    assert len(calls) == 1

    harness.unload("p1")
    assert harness.ctx.optional("svc") is None
    harness.ctx.emit("ping")
    assert len(calls) == 1


def test_failing_plugin_unwinds_partial_registrations():
    harness = Harness()

    def apply(ctx, _config):
        ctx.provide("partial", True)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        harness.use("bad", Plugin("bad", apply))
    assert harness.ctx.optional("partial") is None


def test_duplicate_plugin_id_rejected():
    harness = Harness()
    noop = Plugin("noop", lambda ctx, config: None)
    harness.use("p", noop)
    with pytest.raises(ValueError, match="duplicate"):
        harness.use("p", noop)


def test_middleware_runs_in_registration_order():
    ctx = Context()
    ctx.intercept("call", lambda payload, next_fn: f"a({next_fn(payload)})")
    ctx.intercept("call", lambda payload, next_fn: f"b({next_fn(payload)})")
    assert ctx.invoke("call", "x", lambda payload: f"T:{payload}") == "a(b(T:x))"


def test_disposed_middleware_no_longer_runs():
    ctx = Context()
    dispose = ctx.intercept("call", lambda payload, next_fn: f"a({next_fn(payload)})")
    dispose()
    assert ctx.invoke("call", "x", lambda _payload: "T") == "T"
