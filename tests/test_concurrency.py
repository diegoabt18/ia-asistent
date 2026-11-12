import asyncio
import threading
import time

from app.core.config import Settings
from app.services.research_service import ResearchService
from app.services.verification_service import VerificationResult


class _TrackingVerifier:
    """Verificador falso que registra cuántas verificaciones se ejecutan a la vez."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def verify(self, answer, documents, ignore=()):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        with self._lock:
            self.active -= 1
        return VerificationResult("UNVERIFIED", answer, 0, 0, (), ())


def _service(slots: asyncio.Semaphore, verifier: _TrackingVerifier) -> ResearchService:
    settings = Settings(_env_file=None)
    # Solo se usa verify_limited, que no toca búsqueda, Ollama ni HTTP.
    return ResearchService(
        settings, None, None, None, queries=None, verifier=verifier, verify_slots=slots  # type: ignore[arg-type]
    )


async def _run_all(services: list[ResearchService], count: int) -> list[str]:
    tasks = [services[i % len(services)].verify_limited(f"r{i}", []) for i in range(count)]
    results = await asyncio.gather(*tasks)
    return [r.answer for r in results]


def test_verifications_never_exceed_the_configured_limit() -> None:
    verifier = _TrackingVerifier()
    asyncio.run(_run_all([_service(asyncio.Semaphore(2), verifier)], 8))
    assert verifier.peak == 2


def test_limit_of_one_runs_verifications_sequentially() -> None:
    verifier = _TrackingVerifier()
    asyncio.run(_run_all([_service(asyncio.Semaphore(1), verifier)], 4))
    assert verifier.peak == 1


def test_limit_is_shared_across_service_instances_of_one_process() -> None:
    # Cada petición crea su propio ResearchService; el límite debe ser común a todas.
    verifier = _TrackingVerifier()
    shared = asyncio.Semaphore(2)
    services = [_service(shared, verifier) for _ in range(5)]
    answers = asyncio.run(_run_all(services, 10))
    assert verifier.peak == 2
    assert answers == [f"r{i}" for i in range(10)]


def test_all_verifications_complete_under_the_limit() -> None:
    answers = asyncio.run(_run_all([_service(asyncio.Semaphore(2), _TrackingVerifier())], 5))
    assert answers == [f"r{i}" for i in range(5)]
