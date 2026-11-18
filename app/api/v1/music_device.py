"""Rutas del ESP32. Autenticadas con su propio token (Authorization: Bearer), no con la API key.

Protocolo del firmware:
1. GET /ping al arrancar: comprueba red y token.
2. POST /heartbeat cada pocos segundos (menos que MUSIC_DEVICE_TIMEOUT_SECONDS): informa de su estado
   y recibe el del reproductor. Si `generation` cambió o `status` es "playing" sin stream abierto,
   abre GET /stream.
3. GET /stream: audio de la pista actual (204 si no hay nada). Termina al acabar la pista, al cambiar
   de pista o al detener; en pausa el servidor deja de enviar datos sin cerrar la conexión.
4. POST /control: botones físicos del dispositivo.
"""

import logging

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import StreamingResponse

from app.core.dependencies import get_music_controller
from app.core.security import DeviceId, require_device_token, require_music_enabled
from app.schemas.music import DeviceControlRequest, DevicePingResponse, HeartbeatRequest, MusicStateOut
from app.services.music.commands import to_state
from app.services.music.controller import MusicController
from app.services.music.player import DeviceReport

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/music/device",
    tags=["music-device"],
    dependencies=[Depends(require_music_enabled), Depends(require_device_token)],
)


@router.get("/ping", response_model=DevicePingResponse)
async def ping(device_id: DeviceId, controller: MusicController = Depends(get_music_controller)) -> DevicePingResponse:
    snap = controller.snapshot()
    return DevicePingResponse(ok=True, device_id=device_id, generation=snap.generation, status=snap.status)


@router.post("/heartbeat", response_model=MusicStateOut)
async def heartbeat(
    payload: HeartbeatRequest,
    device_id: DeviceId,
    controller: MusicController = Depends(get_music_controller),
) -> MusicStateOut:
    report = DeviceReport(device_id=device_id, **payload.model_dump())
    return to_state(controller.heartbeat(report))


@router.get("/state", response_model=MusicStateOut)
async def state(device_id: DeviceId, controller: MusicController = Depends(get_music_controller)) -> MusicStateOut:
    return to_state(controller.snapshot())


@router.get("/stream", response_class=StreamingResponse)
async def stream(device_id: DeviceId, controller: MusicController = Depends(get_music_controller)) -> Response:
    session = await controller.open_stream()
    if session is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    logger.info("Stream de %s para %s", session.track.video_id, device_id)
    headers = {
        **session.headers,
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "X-Music-Generation": str(session.generation),
        "X-Track-Id": session.track.video_id,
    }
    return StreamingResponse(session.chunks, media_type=session.media_type, headers=headers)


@router.post("/control", response_model=MusicStateOut)
async def control(
    payload: DeviceControlRequest,
    device_id: DeviceId,
    controller: MusicController = Depends(get_music_controller),
) -> MusicStateOut:
    action = payload.action
    if action == "toggle":
        action = "pause" if controller.snapshot().status == "playing" else "resume"
    if action == "pause":
        await controller.pause()
    elif action == "resume":
        await controller.resume()
    elif action == "next":
        await controller.next()
    elif action == "previous":
        await controller.previous()
    else:
        await controller.stop()
    logger.info("Control %s desde %s", action, device_id)
    return to_state(controller.snapshot())
