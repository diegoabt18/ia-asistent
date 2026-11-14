from fastapi import Request

from app.repositories.conversation_repository import ConversationRepository
from app.services.assistant_service import AssistantService
from app.services.ollama_service import OllamaService
from app.services.research_service import ResearchService
from app.services.search_service import SearchService


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
