"""Rutas de música para el servidor de Lumia (API key + X-User-Ref). La música suena en este servidor."""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.core.config import Settings, get_settings
from app.core.dependencies import get_music_commands, get_music_controller
from app.core.security import UserRef, require_music_enabled
from app.schemas.music import (
    ArtistOut,
    MusicCommandRequest,
    MusicCommandResponse,
    MusicHistoryResponse,
    MusicRecommendationsResponse,
    MusicSearchResponse,
    MusicStateOut,
    MusicToolSpec,
    OutputRequest,
    OutputsResponse,
    QueueAddRequest,
    TrackOut,
)
from app.services.music.commands import (
    InvalidCommandError,
    MusicCommands,
    can_control,
    to_state,
    to_track,
    tool_specs,
    unavailable_text,
)
from app.services.music.controller import AlreadyQueuedError, MusicController, NothingToPlayError
from app.services.music.output import OutputUnavailableError
from app.services.music.queue import QueueFullError
from app.services.music.types import VIDEO_ID_PATTERN

# Eventos del reproductor: se comprueba el estado al cambiar (o cada segundo, para detectar saltos de
# posición) y se envía solo si algo visible cambió. Un comentario cada 15 s mantiene viva la conexión.
EVENTS_CHECK_SECONDS = 1.0
EVENTS_HEARTBEAT_SECONDS = 15.0
# Diferencia (s) entre la posición esperada y la real a partir de la cual se reenvía el estado.
EVENTS_POSITION_DRIFT = 3.0
ARTIST_TRACKS = 6

router = APIRouter(prefix="/music", tags=["music"], dependencies=[Depends(require_music_enabled)])


def _require_control(settings: Settings, user_ref: str) -> None:
    if not can_control(settings, user_ref):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso para controlar la música")


@router.get("/tools", response_model=list[MusicToolSpec])
async def tools() -> list[MusicToolSpec]:
    # Catálogo para que el servidor lo añada a /select según los permisos del usuario.
    return tool_specs()


@router.post("/commands", response_model=MusicCommandResponse)
async def command(
    payload: MusicCommandRequest,
    user_ref: UserRef,
    commands: MusicCommands = Depends(get_music_commands),
    controller: MusicController = Depends(get_music_controller),
) -> MusicCommandResponse:
    try:
        result = await commands.execute(user_ref, payload.tool, payload.args)
    except InvalidCommandError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return MusicCommandResponse(
        tool=result.tool,
        ok=result.ok,
        text=result.text,
        error=result.error,
        tracks=[to_track(t) for t in result.tracks],
        state=to_state(controller.snapshot()),
    )


@router.get("/search", response_model=MusicSearchResponse)
async def search(
    user_ref: UserRef,
    q: str = Query(min_length=1, max_length=200),
    limit: int | None = Query(default=None, ge=1, le=10),
    controller: MusicController = Depends(get_music_controller),
) -> MusicSearchResponse:
    try:
        tracks = await controller.search(q, limit)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return MusicSearchResponse(tracks=[to_track(t) for t in tracks])


@router.get("/state", response_model=MusicStateOut)
async def state(user_ref: UserRef, controller: MusicController = Depends(get_music_controller)) -> MusicStateOut:
    return to_state(controller.snapshot())


def _signature(state: dict[str, Any]) -> str:
    """Lo que identifica un cambio visible (sin la posición, que avanza sola, ni la hora)."""
    return json.dumps({k: v for k, v in state.items() if k not in ("position_seconds", "updated_at", "version")})


@router.get("/events")
async def events(
    request: Request, user_ref: UserRef, controller: MusicController = Depends(get_music_controller)
) -> StreamingResponse:
    """SSE con el estado del reproductor: uno al conectar y otro solo cuando cambia de verdad."""

    async def stream() -> AsyncIterator[str]:
        last_signature: str | None = None
        last_position = 0.0
        last_sent = 0.0
        last_playing = False
        version = -1
        while not await request.is_disconnected():
            state = to_state(controller.snapshot()).model_dump()
            now = time.monotonic()
            expected = last_position + (now - last_sent if last_playing else 0.0)
            signature = _signature(state)
            drift = abs(state["position_seconds"] - expected) > EVENTS_POSITION_DRIFT
            if signature != last_signature or drift:
                yield f"event: state\ndata: {json.dumps(state)}\n\n"
                last_signature, last_position, last_sent = signature, float(state["position_seconds"]), now
                last_playing = state["status"] == "playing"
            elif now - last_sent >= EVENTS_HEARTBEAT_SECONDS:
                yield ": ping\n\n"
                last_position, last_sent = expected, now
            version = await controller.wait_change(version, EVENTS_CHECK_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/recommendations", response_model=MusicRecommendationsResponse)
async def recommendations(
    user_ref: UserRef,
    video_id: str | None = Query(default=None, pattern=VIDEO_ID_PATTERN),
    limit: int = Query(default=12, ge=1, le=25),
    controller: MusicController = Depends(get_music_controller),
) -> MusicRecommendationsResponse:
    """Descubrir: relacionadas reales (Mix de YouTube) y más del mismo artista, para una canción."""
    current = controller.snapshot().current
    seed_id = video_id or (current.video_id if current else None)
    if seed_id is None:
        return MusicRecommendationsResponse(seed=None, tracks=[], artist_tracks=[], artists=[])
    seed = current if current and current.video_id == seed_id else None
    related = await controller.recommendations(seed_id, limit)
    artist = seed.artist if seed else None
    artist_tracks: list = []
    if artist:
        try:
            found = await controller.artist_tracks(artist, ARTIST_TRACKS + 1)
        except ValueError:
            found = []
        known = {seed_id, *(t.video_id for t in related)}
        artist_tracks = [t for t in found if t.video_id not in known][:ARTIST_TRACKS]
    counts: dict[str, int] = {}
    for track in related:
        if track.artist and track.artist != artist:
            counts[track.artist] = counts.get(track.artist, 0) + 1
    artists = [ArtistOut(name=name, tracks=n) for name, n in sorted(counts.items(), key=lambda i: -i[1])]
    return MusicRecommendationsResponse(
        seed=to_track(seed) if seed else None,
        tracks=[to_track(t) for t in related],
        artist_tracks=[to_track(t) for t in artist_tracks],
        artists=artists[:8],
    )


@router.get("/history", response_model=MusicHistoryResponse)
async def history(user_ref: UserRef, controller: MusicController = Depends(get_music_controller)) -> MusicHistoryResponse:
    return MusicHistoryResponse(tracks=[to_track(t) for t in controller.history()])


@router.post("/queue", response_model=TrackOut, status_code=status.HTTP_201_CREATED)
async def add_to_queue(
    payload: QueueAddRequest,
    user_ref: UserRef,
    controller: MusicController = Depends(get_music_controller),
    settings: Settings = Depends(get_settings),
) -> TrackOut:
    _require_control(settings, user_ref)
    try:
        track = await controller.enqueue(query=payload.query, video_id=payload.video_id)
    except NothingToPlayError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except QueueFullError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La cola está llena") from exc
    except AlreadyQueuedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya está en la cola") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return to_track(track)


@router.delete("/queue", status_code=status.HTTP_204_NO_CONTENT)
async def clear_queue(
    user_ref: UserRef,
    controller: MusicController = Depends(get_music_controller),
    settings: Settings = Depends(get_settings),
) -> Response:
    _require_control(settings, user_ref)
    await controller.clear()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/output", response_model=OutputsResponse)
async def get_output(user_ref: UserRef, controller: MusicController = Depends(get_music_controller)) -> OutputsResponse:
    try:
        sinks = await controller.outputs()
    except OutputUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.reason) from exc
    return OutputsResponse(mode=controller.snapshot().output_mode, sinks=sinks)


@router.put("/output", response_model=MusicStateOut)
async def set_output(
    payload: OutputRequest,
    user_ref: UserRef,
    controller: MusicController = Depends(get_music_controller),
    settings: Settings = Depends(get_settings),
) -> MusicStateOut:
    _require_control(settings, user_ref)
    try:
        await controller.set_output(payload.mode)
    except OutputUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=unavailable_text(exc)) from exc
    return to_state(controller.snapshot())
