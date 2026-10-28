"""El despacho atiende a varios negocios a la vez, sin que uno acapare los ciclos."""
import asyncio
from types import SimpleNamespace

from app import dispatch_worker


async def _correr(ctx, ventana: float = 0.25) -> int:
    """Corre `run` un momento y devuelve cuántas atenciones se solaparon como máximo."""
    en_vuelo = 0
    maximo = 0

    async def falso_drain(_ctx) -> bool:
        nonlocal en_vuelo, maximo
        en_vuelo += 1
        maximo = max(maximo, en_vuelo)
        await asyncio.sleep(0.05)
        en_vuelo -= 1
        return True

    original = dispatch_worker.drain_one
    dispatch_worker.drain_one = falso_drain
    try:
        tarea = asyncio.create_task(dispatch_worker.run(ctx))
        await asyncio.sleep(ventana)
        tarea.cancel()
        await asyncio.gather(tarea, return_exceptions=True)
    finally:
        dispatch_worker.drain_one = original
    return maximo


def _ctx(workers: int | None):
    settings = SimpleNamespace() if workers is None else SimpleNamespace(dispatch_workers=workers)
    return SimpleNamespace(settings=settings, dispatch_wake=asyncio.Event())


async def test_con_varios_ciclos_se_atiende_en_paralelo():
    assert await _correr(_ctx(3)) == 3


async def test_un_ciclo_es_el_comportamiento_de_siempre():
    assert await _correr(_ctx(1)) == 1


async def test_sin_ajuste_se_comporta_como_un_ciclo():
    """Un contexto sin `dispatch_workers` (otras pruebas, otros arranques) no cambia."""
    assert await _correr(_ctx(None)) == 1


def test_el_ajuste_por_defecto_es_cuatro_y_se_valida():
    from pydantic import ValidationError

    from app.config import Settings

    assert Settings.model_fields["dispatch_workers"].default == 4
    for malo in (0, 33):
        try:
            Settings(dispatch_workers=malo)
        except ValidationError:
            continue
        raise AssertionError(f"debía rechazar dispatch_workers={malo}")
