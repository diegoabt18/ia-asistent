import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443}


class UnsafeUrlError(ValueError):
    """La URL apunta a un destino que no se debe consultar (protección SSRF)."""


def assert_public_http_url(url: str) -> None:
    """Lanza UnsafeUrlError si la URL no es http(s) pública.

    Bloquea loopback, redes privadas, link-local y otros rangos no globales, para que el
    asistente no pueda usarse para leer servicios internos (Redis, Ollama, Mongo, etc.).
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError("esquema no permitido")
    if not parts.hostname:
        raise UnsafeUrlError("host vacío")
    if parts.username or parts.password:
        raise UnsafeUrlError("credenciales en la URL")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("puerto inválido") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeUrlError("puerto no permitido")

    default_port = 443 if parts.scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(parts.hostname, port or default_port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError("host no resoluble") from exc

    for info in infos:
        address = info[4][0].split("%")[0]  # quita el scope de IPv6 (fe80::1%eth0)
        if not ipaddress.ip_address(address).is_global:
            raise UnsafeUrlError("destino no público")
