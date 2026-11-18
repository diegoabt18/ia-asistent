"""Rutas de música para el servidor de Lumia (API key + X-User-Ref). Ver también music_device.py."""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.core.config import Settings, get_settings
from app.core.dependencies import get_music_commands, get_music_controller
from app.core.security import UserRef, require_music_enabled
from app.schemas.music import (
    MusicCommandRequest,
    MusicCommandResponse,
    MusicSearchResponse,
    MusicStateOut,
    MusicToolSpec,
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
)
from app.services.music.controller import MusicController, NothingToPlayError
from app.services.music.queue import QueueFullError

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
