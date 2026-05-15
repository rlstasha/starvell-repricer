from app.core.network import mask_proxy_url


def test_mask_proxy_url_hides_credentials() -> None:
    masked = mask_proxy_url("http://login:password@1.1.1.1:8000")

    assert masked == "http://***:***@1.1.1.1:8000"
    assert "login" not in masked
    assert "password" not in masked


def test_mask_proxy_url_keeps_direct_readable() -> None:
    assert mask_proxy_url("") == "direct"
