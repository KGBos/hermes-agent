"""The kanban dispatcher defers worker spawns while an update owns the install.

A worker spawned mid-update imports whatever the checkout holds at that moment,
a half-synced tree. The tick still reclaims and promotes; only the spawn is
deferred, and the card stays ``ready`` for the first tick after the update.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import update_lock, venv_sync


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def no_update(monkeypatch, tmp_path):
    """No live update and no pending dependency-sync tail."""
    monkeypatch.setattr(update_lock, "update_in_progress", lambda *a, **k: False)
    monkeypatch.setattr(venv_sync, "completion_pending_path", lambda root: tmp_path / "no-pending-marker")


def _tick(**kwargs):
    """One dispatcher tick over a single ready card; returns (result, spawn calls, status)."""
    spawned: list[str] = []

    def spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 4242

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="alice")
        result = kbd.dispatch_once(conn, spawn_fn=spawn, **kwargs)
        return result, spawned, kb.get_task(conn, tid).status
    finally:
        conn.close()


def test_spawns_when_no_update_is_running(kanban_home, all_assignees_spawnable, no_update):
    """Control: with no update the card is claimed and spawned."""
    result, spawned, _status = _tick()
    assert len(spawned) == 1
    assert len(result.spawned) == 1


def test_live_update_defers_spawns(kanban_home, all_assignees_spawnable, no_update, monkeypatch):
    """An update that owns the checkout holds every spawn; the card stays ready."""
    monkeypatch.setattr(update_lock, "update_in_progress", lambda *a, **k: True)
    result, spawned, status = _tick()
    assert spawned == []
    assert result.spawned == []
    assert status == "ready"


def test_pending_completion_tail_defers_spawns(kanban_home, all_assignees_spawnable, no_update, monkeypatch, tmp_path):
    """The dependency sync has committed but the update's tail has not finished."""
    marker = tmp_path / "source-completion-pending"
    marker.write_text("")
    monkeypatch.setattr(venv_sync, "completion_pending_path", lambda root: marker)
    result, spawned, status = _tick()
    assert spawned == []
    assert result.spawned == []
    assert status == "ready"


def test_spawns_resume_once_the_update_ends(kanban_home, all_assignees_spawnable, no_update, monkeypatch):
    """The deferral is per tick: the same card spawns on the first tick after the update."""
    state = {"updating": True}
    monkeypatch.setattr(update_lock, "update_in_progress", lambda *a, **k: state["updating"])
    spawned: list[str] = []
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1)
        assert spawned == []
        state["updating"] = False
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1)
    finally:
        conn.close()
    assert spawned == [tid]


def test_unreadable_marker_does_not_wedge_the_board(kanban_home, all_assignees_spawnable, no_update, monkeypatch, caplog):
    """A probe that raises OSError reads as 'no update' and is logged, so spawns continue."""
    def broken(*_args, **_kwargs):
        raise OSError("marker unreadable")

    monkeypatch.setattr(update_lock, "update_in_progress", broken)
    with caplog.at_level("WARNING", logger=venv_sync.__name__):
        _result, spawned, _status = _tick()
    assert len(spawned) == 1
    assert "update probe failed" in caplog.text
