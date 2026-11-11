from fastapi import Request

from app.repositories.conversation_repository import ConversationRepository
from app.services.assistant_service import AssistantService
from app.services.ollama_service import OllamaService
from app.services.search_service import SearchService


def get_ollama_service(request: Request) -> OllamaService:
    return OllamaService(request.app.state.settings, request.app.state.http)


def get_search_service(request: Request) -> SearchService:
    return SearchService(request.app.state.settings, request.app.state.http)


def get_conversation_repository(request: Request) -> ConversationRepository:
    return ConversationRepository(request.app.state.session_factory)


def get_assistant_service(request: Request) -> AssistantService:
    state = request.app.state
    return AssistantService(
        state.settings,
        OllamaService(state.settings, state.http),
        ConversationRepository(state.session_factory),
    )
