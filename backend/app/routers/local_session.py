"""Private process-local client presence used by the desktop launcher.

Only the launcher's ephemeral environment token enables these routes. A socket
represents a live browser page; protocol-level disconnects release it even when
the browser suspends JavaScript timers in background tabs.
"""
from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect


@dataclass
class _LocalSession:
    token: str = field(repr=False)
    clients: int = 0
    connected_once: bool = False
    instance_id: str = field(default_factory=lambda: secrets.token_hex(16))


def create_local_session_router() -> APIRouter:
    # Deliberately read the process environment, not .env or saved settings:
    # ordinary development and public deployments do not acquire this lifecycle.
    session = _LocalSession(token=os.environ.get("STOCKVIEW_LOCAL_SESSION_TOKEN", "").strip())
    local_router = APIRouter(prefix="/api/local-session", include_in_schema=False)

    @local_router.get("/status")
    async def status(request: Request) -> dict:
        if not session.token:
            raise HTTPException(status_code=404, detail="Not found")
        authorizations = request.headers.getlist("authorization")
        scheme, _, token = authorizations[0].partition(" ") if len(authorizations) == 1 else ("", "", "")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), session.token.encode()):
            raise HTTPException(status_code=403, detail="Invalid local session")
        return {
            "clients": session.clients,
            "connected_once": session.connected_once,
            "instance_id": session.instance_id,
        }

    @local_router.websocket("/ws")
    async def presence(websocket: WebSocket) -> None:
        tokens = websocket.query_params.getlist("token")
        if not session.token or len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode(), session.token.encode()):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        session.clients += 1
        session.connected_once = True
        try:
            await websocket.send_json({"type": "ready", "instance_id": session.instance_id})
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            session.clients -= 1

    return local_router


router = create_local_session_router()
