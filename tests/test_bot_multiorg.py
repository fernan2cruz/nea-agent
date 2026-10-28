"""Spec 065: despacho firmado → turno aislado → /api/bot/* y proxy IA."""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from app.crm_bot_org import BotOrgCrmClient
from app.crm_brains import BrainsCrmClient
from app.dispatch_worker import drain_one
from app.followup import FollowupWorker
from app.main import create_app
from app.multiorg import (
    CrmSinOrganizacion,
    LlmSinOrganizacion,
    RegistroDeOrganizaciones,
    credencial_derivada,
)
from app.state import AppContext, MemoryStore, utcnow
from tests.conftest import CRM_URL, IDENTITY, crm_context, evento_despacho, firmar, make_settings, mock_crm_basics

SECRET = "0123456789abcdef0123456789abcdef"


def test_vector_del_crm_generado_con_node():
    # Ejecutado con Node, como src/server/bot/org-credential.ts:
    # node -e "const {createHmac}=require('node:crypto'); console.log(
    # createHmac('sha256','0123456789abcdef0123456789abcdef')
    # .update('vocero:cerebro:v1:org_abc123').digest('base64url'))"
    assert credencial_derivada(SECRET, "org_abc123") == (
        "35xQLucxgNu2V_i1FU6XZE9x9gYYH7W5CaTS4HoN2NI"
    )


@pytest.mark.parametrize("surface", ["", "brains", "bot"])
async def test_superficie_y_secreto_sin_cambiar_el_modo_anterior(surface):
    settings = make_settings(
        vocero_mode="cloud", crm_organization="", crm_surface=surface,
        crm_bot_api_key=SECRET, crm_brain_secret="secreto-brains",
    )
    assert settings.multi_org
    assert settings.dispatch_secret == (SECRET if surface == "bot" else "secreto-brains")
    reg = RegistroDeOrganizaciones(CRM_URL, settings.dispatch_secret, surface=surface)
    try:
        client = reg.cliente("org_abc123", "slug-distinto")
        assert isinstance(client, BotOrgCrmClient if surface == "bot" else BrainsCrmClient)
        assert client._http.headers["X-Vocero-Organization"] == (
            "org_abc123" if surface == "bot" else "slug-distinto"
        )
    finally:
        await reg.aclose()


def contexto():
    settings = make_settings(
        vocero_mode="cloud", crm_surface="bot", crm_organization="",
        crm_bot_api_key=SECRET, crm_brain_secret="no-usar-brains",
        llm_api_key="sk-no-usar-la-llave-del-dueño", llm_model="modelo-respaldo",
    )
    return AppContext(
        settings=settings, store=MemoryStore(), crm=CrmSinOrganizacion(),
        llm=LlmSinOrganizacion(),
        registro=RegistroDeOrganizaciones(CRM_URL, settings.dispatch_secret, surface="bot"),
    )


async def enviar(client, ctx, org, texto, numero):
    payload = evento_despacho(text=texto, wamid=f"m-{org}-{numero}", conv_id=f"cv-{org}")
    payload["organization"] = {"id": org, "slug": f"slug-{org}"}
    body = json.dumps(payload).encode()
    response = await client.post(
        "/vocero/dispatch", content=body,
        headers={"x-vocero-signature": firmar(body, SECRET)},
    )
    assert response.status_code == 200
    assert await drain_one(ctx)


def proxy_ok(request):
    org = request.headers["X-Vocero-Organization"]
    assert org in {"org_a", "org_b"}
    assert request.headers["Authorization"] == f"Bearer {credencial_derivada(SECRET, org)}"
    assert "X-API-Key" not in request.headers
    return httpx.Response(200, json={
        "id": "chat-test", "object": "chat.completion", "created": 0,
        "model": "modelo-perfil", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": f"Respuesta de {org}."}}],
    })


def perfil(respx_mock, llm=None):
    return respx_mock.get(f"{CRM_URL}/api/bot/profile").respond(200, json={
        "profile": {"name": "Nea"}, "kb": "", "resources": [], "llm": llm,
    })


async def test_headers_reales_y_formas_bot_sin_transformar(respx_mock):
    routes = mock_crm_basics(respx_mock)
    reg = RegistroDeOrganizaciones(CRM_URL, SECRET, surface="bot")
    client = reg.cliente("org_abc123", "esto-no-es-el-id")
    try:
        client.registrar(IDENTITY, "cv_test1")
        assert await client.get_context(IDENTITY) == crm_context()
        client.registrar_despacho("cv_test1", "dsp-test")
        assert client.despacho_de("cv_test1") == "dsp-test"
        await client.send_message("cv_test1", "hola")
        request = routes["messages"].calls.last.request
        assert request.headers["Authorization"] == (
            "Bearer 35xQLucxgNu2V_i1FU6XZE9x9gYYH7W5CaTS4HoN2NI"
        )
        assert request.headers["X-Vocero-Organization"] == "org_abc123"
        assert "X-API-Key" not in request.headers
        assert json.loads(request.content) == {"conversationId": "cv_test1", "text": "hola"}
    finally:
        await reg.aclose()


async def test_conversacion_multiturno_dos_negocios_aislados(respx_mock):
    ctx = contexto()
    routes = mock_crm_basics(respx_mock)
    routes["context"].mock(side_effect=lambda request: httpx.Response(
        200, json=crm_context(conv_id=request.url.params["conversationId"])
    ))
    profile = perfil(respx_mock, {"path": "/api/bot/llm", "model": "modelo-perfil"})
    proxy = respx_mock.post(f"{CRM_URL}/api/bot/llm/chat/completions").mock(side_effect=proxy_ok)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://nea.test"
        ) as client:
            await enviar(client, ctx, "org_a", "Busco zapatos rojos", 1)
            await enviar(client, ctx, "org_b", "Busco una consulta dental", 1)
            await enviar(client, ctx, "org_a", "Los necesito talla 40", 2)
        assert proxy.call_count == 3
        assert routes["messages"].call_count == 3
        assert profile.call_count == 2  # TTL independiente por negocio
        assert routes["context"].call_count == 3  # precarga consumida una sola vez
        calls = [json.loads(c.request.content) for c in proxy.calls]
        assert all(c["model"] == "modelo-perfil" for c in calls)
        assert "zapatos rojos" not in json.dumps(calls[1], ensure_ascii=False)
        assert "consulta dental" not in json.dumps(calls[2], ensure_ascii=False)
        assert "zapatos rojos" in json.dumps(calls[2], ensure_ascii=False)
        a = await ctx.store.get_or_create_conversation(IDENTITY, "org_a", "slug-org_a")
        b = await ctx.store.get_or_create_conversation(IDENTITY, "org_b", "slug-org_b")
        assert a.id != b.id
        assert ctx.registro.cliente("org_a", "a") is not ctx.registro.cliente("org_b", "b")
        assert ctx.registro.perfil("org_a", "a") is not ctx.registro.perfil("org_b", "b")
    finally:
        await ctx.registro.aclose()


@pytest.mark.parametrize("status,code", [(409, "org_ambiguous"), (401, "unauthorized")])
async def test_fallo_contexto_calla_y_entrega_a_humano(respx_mock, status, code):
    ctx = contexto()
    routes = mock_crm_basics(respx_mock)
    routes["context"].respond(status, json={"error": {"code": code}})
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://nea.test"
        ) as client:
            await enviar(client, ctx, "org_a", "hola", 1)
        assert routes["messages"].call_count == 0
        assert routes["handoff"].call_count == 1
        request = routes["handoff"].calls.last.request
        assert json.loads(request.content) == {"conversationId": "cv-org_a", "reason": "error"}
        assert request.headers["X-Vocero-Organization"] == "org_a"
    finally:
        await ctx.registro.aclose()


@pytest.mark.parametrize("status,code", [(409, "org_ambiguous"), (401, "unauthorized"), (402, "sin_credencial")])
async def test_fallo_proxy_sin_fallback_a_llave_local(respx_mock, monkeypatch, status, code):
    from app.llm import OpenAiLlm
    monkeypatch.setattr(OpenAiLlm, "RETRIES", 0)
    ctx = contexto()
    routes = mock_crm_basics(respx_mock)
    perfil(respx_mock)  # Sin modelo → LLM_MODEL, siempre por el proxy.
    proxy = respx_mock.post(f"{CRM_URL}/api/bot/llm/chat/completions").respond(
        status, json={"error": {"code": code, "message": "fallo del CRM"}},
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://nea.test"
        ) as client:
            await enviar(client, ctx, "org_a", "hola", 1)
        assert proxy.call_count == 1
        request = proxy.calls.last.request
        assert json.loads(request.content)["model"] == "modelo-respaldo"
        assert request.headers["Authorization"] == f"Bearer {credencial_derivada(SECRET, 'org_a')}"
        assert routes["messages"].call_count == 0
        assert json.loads(routes["handoff"].calls.last.request.content)["reason"] == "error"
    finally:
        await ctx.registro.aclose()


async def test_modelo_se_relee_tras_ttl_y_no_inventa_modelo_de_audio(respx_mock):
    ctx = contexto()
    route = perfil(respx_mock, {"model": "primero", "path": "/api/bot/llm"})
    try:
        reg = ctx.registro
        config = await reg.config_llm("org_a", "a")
        first = reg.llm("org_a", "a", config, ctx.settings)
        assert first._model == "primero"
        assert first._transcribe_model == ""
        route.respond(200, json={"profile": {}, "llm": {"model": "segundo"}})
        reg.perfil("org_a", "a")._fetched_at = 0
        config = await reg.config_llm("org_a", "a")
        second = reg.llm("org_a", "a", config, ctx.settings)
        assert second is not first
        assert second._model == "segundo"
        assert str(second._client.base_url) == f"{CRM_URL}/api/bot/llm/"
    finally:
        await ctx.registro.aclose()


async def test_arranque_bot_usa_secreto_bot_y_centinelas(monkeypatch):
    from app import main
    from tests.test_multiorg import _StoreDePrueba, _WorkerQuieto

    for name, value in {
        "VOCERO_MODE": "cloud", "CRM_SURFACE": "bot", "CRM_ORGANIZATION": "",
        "CRM_BASE_URL": CRM_URL, "CRM_BOT_API_KEY": SECRET,
        "CRM_BRAIN_SECRET": "no-usar", "LLM_API_KEY": "", "OPENAI_API_KEY": "",
        "DATABASE_URL": "postgres://no-se-usa",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main, "PgStore", lambda *_a, **_k: _StoreDePrueba())
    monkeypatch.setattr(main, "FollowupWorker", _WorkerQuieto)
    monkeypatch.setattr(main, "SenderWorker", _WorkerQuieto)
    app = main.create_app()
    async with app.router.lifespan_context(app):
        ctx = app.state.ctx
        assert isinstance(ctx.crm, CrmSinOrganizacion)
        assert isinstance(ctx.llm, LlmSinOrganizacion)
        client = ctx.registro.cliente("org_abc123", "slug-distinto")
        assert isinstance(client, BotOrgCrmClient)
        assert client._http.headers["Authorization"] == (
            "Bearer 35xQLucxgNu2V_i1FU6XZE9x9gYYH7W5CaTS4HoN2NI"
        )


async def test_ruta_interna_de_seguimiento_usa_proxy_de_la_organizacion(respx_mock):
    from app.multiorg import ctx_de_organizacion
    ctx = contexto()
    routes = mock_crm_basics(respx_mock)
    perfil(respx_mock, {"model": "modelo-perfil"})
    proxy = respx_mock.post(f"{CRM_URL}/api/bot/llm/chat/completions").mock(side_effect=proxy_ok)
    conv = await ctx.store.get_or_create_conversation(IDENTITY, "org_b", "slug-org_b")
    await ctx.store.add_message(conv.id, "user", "Me interesa, luego te digo")
    await ctx.store.add_message(conv.id, "assistant", "¿Cuándo te viene bien?")
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id="cv-org_b", greeted=True,
        followup_due_at=utcnow() - timedelta(hours=1),
    )
    try:
        # El barrido cloud sigue desactivado: no se añade un segundo emisor.
        await FollowupWorker(ctx).tick()
        assert proxy.call_count == 0
        resuelto = ctx_de_organizacion(ctx, "org_b", "slug-org_b")
        await FollowupWorker(ctx)._push(resuelto, conv, conv.last_inbound_at)
        assert proxy.call_count == 1
        assert routes["messages"].calls.last.request.headers["X-Vocero-Organization"] == "org_b"
    finally:
        await ctx.registro.aclose()
