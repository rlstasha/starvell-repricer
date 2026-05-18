from app.check_proxy_connectivity import (
    ConnectivityAttempt,
    _exception_chain,
    classify_stability,
    diagnose,
    parse_proxy_endpoint,
)


def test_parse_proxy_endpoint_hides_credentials_from_public_fields() -> None:
    endpoint = parse_proxy_endpoint("socks5://login:password@1.2.3.4:1080")

    assert endpoint is not None
    assert endpoint.scheme == "socks5"
    assert endpoint.host == "1.2.3.4"
    assert endpoint.port == 1080
    assert endpoint.username == "login"
    assert endpoint.password == "password"


def test_exception_chain_masks_proxy_credentials() -> None:
    exc = RuntimeError("socks5://login:password@1.2.3.4:1080 failed")

    text = _exception_chain(exc)

    assert "login" not in text
    assert "password" not in text
    assert "socks5://***:***@1.2.3.4:1080" in text


def test_classify_stability() -> None:
    assert classify_stability(3, 3) == "stable (3/3 ok)"
    assert classify_stability(0, 3) == "failing (0/3 ok)"
    assert classify_stability(2, 3) == "unstable (2/3 ok)"


def test_diagnose_detects_intermittent_socks_malformed_replies() -> None:
    attempts = [
        ConnectivityAttempt(
            connection="ok",
            handshake="ok",
            external_ip="1.2.3.4",
            response="HTTP/1.1 200 OK",
            exact_exception=None,
            elapsed_ms=100,
        ),
        ConnectivityAttempt(
            connection="ok",
            handshake="failed",
            external_ip=None,
            response="no usable response",
            exact_exception="Socks5HandshakeError: Malformed reply during connect: 48545450",
            elapsed_ms=50,
        ),
    ]

    assert diagnose(attempts) == (
        "proxy provider unstable: intermittent SOCKS5 malformed replies"
    )


def test_diagnose_detects_failing_socks_handshake() -> None:
    attempts = [
        ConnectivityAttempt(
            connection="ok",
            handshake="failed",
            external_ip=None,
            response="no usable response",
            exact_exception="socksio.exceptions.ProtocolError: Malformed reply",
            elapsed_ms=50,
        ),
    ]

    assert diagnose(attempts) == (
        "proxy provider failing: SOCKS5 handshake returns malformed replies"
    )
