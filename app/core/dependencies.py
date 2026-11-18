from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.repositories.conversation_repository import ConversationRepository
from app.services.assistant_service import AssistantService
from app.services.music.commands import MusicCommands
from app.services.music.controller import MusicController
from app.services.ollama_service import OllamaService
from app.services.research_service import ResearchService
from app.services.search_service import SearchService
from app.services.selector_service import SelectorService


def _ollama(state) -> OllamaService:
    # Todas las llamadas al modelo comparten el semáforo del proceso (ver main.lifespan).
    return OllamaService(state.settings, state.http, getattr(state, "model_slots", None))


def get_ollama_service(request: Request) -> OllamaService:
    return _ollama(request.app.state)


def get_search_service(request: Request) -> SearchService:
    return SearchService(request.app.state.settings, request.app.state.http)


def get_conversation_repository(request: Request) -> ConversationRepository:
    return ConversationRepository(request.app.state.session_factory)


def get_research_service(request: Request) -> ResearchService:
    state = request.app.state
    return ResearchService(
        state.settings,
        SearchService(state.settings, state.http),
        _ollama(state),
        state.http,
        verify_slots=state.verify_slots,
    )


def get_assistant_service(request: Request) -> AssistantService:
    state = request.app.state
    return AssistantService(
        state.settings,
        _ollama(state),
        ConversationRepository(state.session_factory),
        get_research_service(request),
    )


def get_music_controller(request: Request) -> MusicController:
    # Una sola instancia por proceso (estado de reproducción compartido), creada en main.lifespan.
    return request.app.state.music


def get_music_commands(
    controller: MusicController = Depends(get_music_controller),
    settings: Settings = Depends(get_settings),
) -> MusicCommands:
    return MusicCommands(settings, controller)


def get_selector_service(request: Request) -> SelectorService:
    state = request.app.state
    return SelectorService(state.settings, _ollama(state), ConversationRepository(state.session_factory))
