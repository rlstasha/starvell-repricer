import json

from app.check_starvell_socket import (
    STARVELL_BROWSER_NAMESPACES,
    make_socketio_event_frame,
    make_socketio_connect_frame,
    namespaces_for_args,
    parse_args,
    parse_csv,
    probe_namespaces,
    probe_payloads,
    sanitize,
    socketio_connect_namespaces,
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


def test_make_socketio_connect_frame_for_default_namespace() -> None:
    assert make_socketio_connect_frame("/") == "40"


def test_make_socketio_connect_frame_for_custom_namespace() -> None:
    assert make_socketio_connect_frame("/viewed-offers") == "40/viewed-offers,"


def test_namespaces_for_all_browser_namespaces() -> None:
    args = parse_args(["--all-namespaces"])

    assert namespaces_for_args(args) == list(STARVELL_BROWSER_NAMESPACES)
    assert "/orders" in namespaces_for_args(args)


def test_namespaces_can_include_experimental_offers_namespace() -> None:
    args = parse_args(["--namespace", "/viewed-offers", "--market-namespaces"])

    assert namespaces_for_args(args) == ["/viewed-offers", "/offers", "/"]


def test_probe_namespaces_prefers_viewed_offers_and_default() -> None:
    assert probe_namespaces(["/", "/chats", "/viewed-offers"]) == ["/viewed-offers", "/"]


def test_socketio_connect_namespaces_skips_experimental_offers_namespace() -> None:
    assert socketio_connect_namespaces(["/", "/viewed-offers", "/offers"]) == ["/", "/viewed-offers"]


def test_parse_csv_strips_empty_values() -> None:
    assert parse_csv("1996, 1998,,2000 ") == ["1996", "1998", "2000"]


def test_probe_payloads_include_viewed_offers_variants() -> None:
    payloads = probe_payloads(
        "viewed-offers",
        lot_ids=["1996", "2000"],
        category_ids=["65"],
    )

    assert ("viewed-offers", {}) in payloads
    assert ("join", {"room": "viewed-offers"}) in payloads
    assert ("join", "offers") in payloads
    assert ("subscribe", {"room": "offers"}) in payloads
    assert ("subscribe", {"channel": "viewed-offers"}) in payloads
    assert ("subscribe", {"lot_ids": ["1996", "2000"]}) in payloads
    assert ("subscribe", {"lotIds": [1996, 2000]}) in payloads
    assert ("subscribe", {"offer_ids": ["1996", "2000"]}) in payloads
    assert ("offers", {}) in payloads
    assert ("offer_subscribe", {"offerIds": [1996, 2000]}) in payloads
    assert ("offers_subscribe", {"offerIds": [1996, 2000]}) in payloads
    assert ("price_subscribe", {"offerIds": [1996, 2000]}) in payloads
    assert ("market_subscribe", {"subCategoryIds": [65]}) in payloads
    assert ("category_subscribe", {"categoryIds": [65]}) in payloads
    assert ("viewed_offer_subscribe", {"offerIds": [1996, 2000]}) in payloads
    assert ("watch", {"room": "offers"}) in payloads
    assert ("watch", {"offerId": 1996}) in payloads
    assert ("watch", {"subCategoryIds": [65]}) in payloads
    assert ("subscribe", {"category_ids": ["65"]}) in payloads


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
