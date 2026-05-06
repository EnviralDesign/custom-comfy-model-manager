"""WebSocket handler for realtime updates."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import json
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

# Connected clients and their per-client send locks. A transfer can trigger
# broadcasts from several async tasks; Starlette/Uvicorn expects writes to a
# single WebSocket to be serialized.
_clients: dict[WebSocket, asyncio.Lock] = {}
_clients_lock = asyncio.Lock()


async def _add_client(websocket: WebSocket) -> None:
    async with _clients_lock:
        _clients[websocket] = asyncio.Lock()


async def _remove_client(websocket: WebSocket) -> None:
    async with _clients_lock:
        _clients.pop(websocket, None)


async def broadcast(event_type: str, data: dict):
    """Broadcast an event to all connected clients."""
    message = json.dumps({"type": event_type, "data": data})

    async with _clients_lock:
        clients = list(_clients.items())

    disconnected: list[WebSocket] = []
    for client, send_lock in clients:
        try:
            async with send_lock:
                await client.send_text(message)
        except WebSocketDisconnect:
            disconnected.append(client)
        except RuntimeError as exc:
            logger.debug("Dropping closed WebSocket client: %s", exc)
            disconnected.append(client)
        except Exception:
            logger.exception("Dropping WebSocket client after broadcast failure")
            disconnected.append(client)

    if disconnected:
        async with _clients_lock:
            for client in disconnected:
                _clients.pop(client, None)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for realtime updates."""
    await websocket.accept()
    await _add_client(websocket)

    try:
        while True:
            # Keep connection alive, handle incoming messages if needed
            await websocket.receive_text()
    except WebSocketDisconnect:
        await _remove_client(websocket)
    except Exception:
        logger.exception("WebSocket connection failed")
        await _remove_client(websocket)
