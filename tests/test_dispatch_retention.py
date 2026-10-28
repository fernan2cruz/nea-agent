import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.dispatch_store import MemoryDispatchStore, PgDispatchStore
from app.dispatch_worker import _purga_periodica


async def test_purga_solo_terminados_antiguos():
    store = MemoryDispatchStore()
    old = datetime.now(timezone.utc) - timedelta(days=15)
    recent = datetime.now(timezone.utc)
    for key, state, date in [("viejo", "done", old), ("nuevo", "done", recent),
                             ("pendiente", "queued", old), ("en_curso", "started", old)]:
        await store.enqueue_dispatch("org", {"dispatchId": key, "conversation": {"id": "cv"}})
        store.dispatch_inbox[("org", key)].update(state=state, updated_at=date)
    assert await store.purge_dispatches(14) == 1
    assert set(store.dispatch_inbox) == {("org", "nuevo"), ("org", "pendiente"), ("org", "en_curso")}


async def test_purga_pg_acotada_sin_tocar_trabajo_vivo():
    store = PgDispatchStore()
    store.pool = SimpleNamespace(fetch=AsyncMock(return_value=[{"dispatch_id": "d"}]))
    assert await store.purge_dispatches(7) == 1
    query, days = store.pool.fetch.call_args.args
    assert days == 7
    assert "state='done'" in query and "updated_at <" in query
    assert "LIMIT 1000 FOR UPDATE SKIP LOCKED" in query


async def test_purga_periodica_sobrevive_fallos_y_espera_un_dia(monkeypatch, caplog):
    store = SimpleNamespace(purge_dispatches=AsyncMock(side_effect=[RuntimeError("privado"), 1000, 0]))
    ctx = SimpleNamespace(store=store, settings=SimpleNamespace(dispatch_retention_days=7))
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if sleeps.count(86400) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.dispatch_worker.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await _purga_periodica(ctx)
    assert store.purge_dispatches.await_count == 3
    assert sleeps == [86400, 0, 86400]
    assert "privado" not in caplog.text


def test_setting_retencion_valida():
    assert Settings(_env_file=None).dispatch_retention_days == 14
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dispatch_retention_days=0)
