"""WebSocket handler for realtime updates."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Set
import asyncio
import json

router = APIRouter()

# Connected clients
_clients: Set[WebSocket] = set()


async def broadcast(event_type: str, data: dict):
    """Broadcast an event to all connected clients."""
    message = json.dumps({"type": event_type, "data": data})
    disconnected = set()
    for client in _clients:
        try:
            await client.send_text(message)
        except:
            disconnected.add(client)
    _clients.difference_update(disconnected)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for realtime updates."""
    await websocket.accept()
    _clients.add(websocket)
    
    try:
        while True:
            # Keep connection alive, handle incoming messages if needed
            data = await websocket.receive_text()
            # Echo or handle commands here
    except WebSocketDisconnect:
        _clients.discard(websocket)
    except Exception:
        _clients.discard(websocket)
