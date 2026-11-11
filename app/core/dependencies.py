from fastapi import Request

from app.services.ollama_service import OllamaService
from app.services.search_service import SearchService


def get_ollama_service(request: Request) -> OllamaService:
    return OllamaService(request.app.state.settings, request.app.state.http)


def get_search_service(request: Request) -> SearchService:
    return SearchService(request.app.state.settings, request.app.state.http)
