import json

from app.check_starvell_socket import (
    make_socketio_event_frame,
    sanitize,
    split_engineio_packet,
    split_socketio_connect_frame,
    split_socketio_event_frame,
)


def test_split_engineio_open_packet() -> None:
    packet_type, payload = split_engineio_packet('0{"sid":"abc","pingInterval":25000}')

    assert packet_type == "0"
    assert json.loads(payload)["sid"] == "abc"


def test_split_socketio_event_with_namespace() -> None:
    namespace, payload = split_socketio_event_frame('/viewed-offers,["offer:update",{"id":2000}]')

    assert namespace == "/viewed-offers"
    assert json.loads(payload)[0] == "offer:update"


def test_split_socketio_connect_with_namespace_payload() -> None:
    namespace, payload = split_socketio_connect_frame('/viewed-offers,{"sid":"abc"}')

    assert namespace == "/viewed-offers"
    assert payload == {"sid": "abc"}


def test_split_socketio_connect_default_namespace_payload() -> None:
    namespace, payload = split_socketio_connect_frame('{"sid":"abc"}')

    assert namespace == "/"
    assert payload == {"sid": "abc"}


def test_make_socketio_event_frame_for_default_namespace() -> None:
    assert make_socketio_event_frame("join", {"room": "viewed-offers"}, namespace="/") == (
        '42["join",{"room":"viewed-offers"}]'
    )


def test_make_socketio_event_frame_for_custom_namespace() -> None:
    assert make_socketio_event_frame(
        "viewed-offers",
        {"room": "viewed-offers"},
        namespace="/viewed-offers",
    ) == '42/viewed-offers,["viewed-offers",{"room":"viewed-offers"}]'


def test_sanitize_hides_secret_fields() -> None:
    payload = {
        "event": "ok",
        "cookie": "secret",
        "nested": {"csrfToken": "secret", "value": 123},
    }

    assert sanitize(payload) == {
        "event": "ok",
        "cookie": "***",
        "nested": {"csrfToken": "***", "value": 123},
    }
