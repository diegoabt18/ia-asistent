import pytest

from app.services.url_guard import UnsafeUrlError, assert_public_http_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/archivo",
        "http://127.0.0.1/",
        "http://10.0.0.5/admin",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "https://usuario:clave@example.com/",
        "http://example.com:6379/",
    ],
)
def test_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_http_url(url)
