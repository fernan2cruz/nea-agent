"""Superficie /api/bot/* con credencial derivada y organización por ID."""
from __future__ import annotations

from typing import Any

import httpx

from app.crm import CrmClient, CrmError


class BotOrgCrmClient(CrmClient):
    supports_agenda_v2 = False
    supports_coordination = False

    def __init__(
        self,
        base_url: str,
        secret: str,
        organization: str,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url, secret, timeout, client)
        # X-API-Key tiene prioridad en el CRM: no dejar la llave heredada.
        self._http.headers.pop("X-API-Key", None)
        self._http.headers.update({
            "Authorization": f"Bearer {secret}",
            "X-Vocero-Organization": organization,
        })
        self._precargado: dict[str, dict[str, Any]] = {}
        self._despachos: dict[str, str] = {}

    def registrar_despacho(self, conversation_id: str, dispatch_id: str) -> None:
        """Recuerda el despacho para envíos diferidos, sin alterar el cuerpo bot."""
        if dispatch_id:
            self._despachos[conversation_id] = dispatch_id

    def despacho_de(self, conversation_id: str) -> str:
        return self._despachos.get(conversation_id, "")

    async def precargar(self, conversation_id: str) -> dict[str, Any] | None:
        self._precargado.pop(conversation_id, None)
        try:
            resp = await self._request(
                "GET", "/api/bot/context", params={"conversationId": conversation_id}
            )
        except CrmError:
            return None
        if resp.status_code != 200:
            return None
        data: dict[str, Any] = resp.json()
        self._precargado[conversation_id] = data
        return data

    async def get_context(self, wa_identity: str) -> dict[str, Any] | None:
        conversation_id = self._conversaciones.get(wa_identity, "")
        data = self._precargado.pop(conversation_id, None)
        if data is not None:
            return data
        return await super().get_context(wa_identity)

    def credenciales_llm(self) -> dict[str, Any]:
        # La selección del modelo se resuelve por el perfil con TTL en el registro.
        return {"path": "/api/bot/llm"}
