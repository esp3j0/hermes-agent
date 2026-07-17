"""P0 multi-backend: per-call backend selection, routing, and instance isolation.

End-to-end through the real terminal_tool → resolve_env_key → (task, backend)
cache → BaseEnvironment → subprocess path (two local backends, no ssh/docker).
Pins the three P0 guarantees:
  1. terminal(backend=...) routes to the right backend
  2. each backend holds independent env state (snapshot isolation)
  3. invalid backend is rejected by the allowlist without touching the host
  4. omitting backend routes to default
"""
import json
import pytest

import tools.terminal_tool as tt


def _two_local_backends():
    base = {"env": "local", "cwd": "", "timeout": 30, "risk_level": 1,
            "approval": "high_risk", "force_allowed": True, "deny": [], "allow_only": []}
    return ({"this": dict(base), "wk": dict(base)}, "this")


@pytest.fixture
def isolated_backends(monkeypatch):
    """Reset all global env state and pin a two-backend config (no host config)."""
    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(tt, "_creation_locks", {})
    monkeypatch.setattr(tt, "_load_backends_config", _two_local_backends)
    monkeypatch.setattr(tt, "_check_all_guards", lambda *a, **k: {"approved": True})
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)


def _run(command, backend=None, task_id="default"):
    kw = {"command": command, "task_id": task_id}
    if backend is not None:
        kw["backend"] = backend
    r = json.loads(tt.terminal_tool(**kw))
    return (r.get("output") or "").strip(), r.get("exit_code")


def test_routing_and_state_isolation_between_backends(isolated_backends):
    _run("export MARK=alpha", backend="this")
    _run("export MARK=beta", backend="wk")
    this_mark, _ = _run("echo $MARK", backend="this")
    wk_mark, _ = _run("echo $MARK", backend="wk")
    assert this_mark == "alpha"
    assert wk_mark == "beta"
    keys = list(tt._active_environments.keys())
    assert ("default", "this") in keys
    assert ("default", "wk") in keys


def test_invalid_backend_rejected_without_touching_host(isolated_backends):
    before = dict(tt._active_environments)
    _out, ec = _run("echo SHOULD_NOT_RUN", backend="evil_host")
    assert ec != 0
    assert tt._active_environments == before  # no env created — allowlist held


def test_omitted_backend_routes_to_default(isolated_backends):
    _run("export MARK=alpha", backend="this")
    default_mark, _ = _run("echo $MARK")  # no backend → default 'this'
    assert default_mark == "alpha"
