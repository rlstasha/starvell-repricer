from app.discover_starvell_market_sources import (
    BenchmarkSummary,
    EndpointCandidate,
    FetchResult,
    benchmark_summary_to_dict,
    dedupe_candidates,
    extract_market_source_candidates,
    extract_next_build_id,
    extract_script_sources,
    mask_secret_text,
    percentile,
    safe_discovered_get_candidates,
    summarize_results,
)


def test_extract_next_build_id_from_next_data() -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"buildId":"build-123","props":{}}'
        "</script>"
    )

    assert extract_next_build_id(html) == "build-123"


def test_extract_script_sources_keeps_next_static_assets() -> None:
    html = """
    <script src="/_next/static/chunks/main.js"></script>
    <script src="https://starvell.com/_next/static/chunks/page.js?d=1"></script>
    <script src="https://example.com/_next/static/ignore.js"></script>
    """

    assert extract_script_sources(html, base_url="https://starvell.com") == [
        "/_next/static/chunks/main.js",
        "/_next/static/chunks/page.js?d=1",
    ]


def test_extract_market_source_candidates_from_js() -> None:
    js = """
    fetch("/api/offers/list-by-category", {method: "POST"})
    axios.get("/api/offers/details?id=1")
    socket.emit("offer_subscribe", { offerIds: [2000] })
    socket.on("price:update", handler)
    const es = new EventSource("/api/offers/stream")
    io("/viewed-offers")
    """

    summary = extract_market_source_candidates(js, source="chunk.js")
    endpoints = {(item.method, item.url) for item in summary.endpoints}

    assert ("POST", "/api/offers/list-by-category") in endpoints
    assert ("GET", "/api/offers/details?id=1") in endpoints
    assert ("GET", "/api/offers/stream") in endpoints
    assert "offer_subscribe" in summary.socket_events
    assert "price:update" in summary.socket_events
    assert "namespace:/viewed-offers" in summary.socket_events
    assert summary.eventsource_urls == ["/api/offers/stream"]


def test_safe_discovered_get_candidates_skips_mutating_api() -> None:
    candidates = [
        EndpointCandidate("GET", "/api/offers/2000/update", "js"),
        EndpointCandidate("GET", "/api/offers/details?id=1", "js"),
        EndpointCandidate("POST", "/api/offers/list-by-category", "js"),
    ]

    safe = safe_discovered_get_candidates(candidates)

    assert [item.url for item in safe] == ["/api/offers/details?id=1"]


def test_dedupe_candidates_normalizes_absolute_starvell_urls() -> None:
    candidates = [
        EndpointCandidate("GET", "https://starvell.com/offers/2000", "a"),
        EndpointCandidate("GET", "/offers/2000", "b"),
    ]

    assert dedupe_candidates(candidates) == [EndpointCandidate("GET", "/offers/2000", "a", "")]


def test_summarize_results_calculates_avg_p95_errors_and_size() -> None:
    source = EndpointCandidate("GET", "/offers/2000", "test", "offer_page")
    results = [
        FetchResult("a", "GET", "/offers/2000", 200, 10.0, 100, {"cache-control": "no-store"}, useful_hit=True),
        FetchResult("a", "GET", "/offers/2000", 200, 20.0, 120, {}, useful_hit=True),
        FetchResult("a", "GET", "/offers/2000", 500, 30.0, 10, {}, useful_hit=False),
    ]

    summary = summarize_results(source, results)

    assert summary.avg_ms == 20.0
    assert summary.p95_ms == 30.0
    assert summary.response_size_avg == 77
    assert summary.errors == 1
    assert summary.useful_hits == 2
    assert summary.status_codes == {200: 2, 500: 1}
    assert benchmark_summary_to_dict(summary)["name"] == "offer_page"


def test_mask_secret_text_hides_credentials() -> None:
    text = "cookie=session123 authorization=Bearer abc http://login:pass@1.1.1.1:8000"

    assert mask_secret_text(text) == "cookie=*** authorization=*** http://***:***@1.1.1.1:8000"


def test_percentile_handles_empty_and_single_values() -> None:
    assert percentile([], 95) == 0.0
    assert percentile([12.5], 95) == 12.5


def test_benchmark_summary_dataclass_is_importable() -> None:
    summary = BenchmarkSummary(
        name="x",
        method="GET",
        url="/x",
        requests=1,
        avg_ms=1.0,
        p95_ms=1.0,
        response_size_avg=10,
        errors=0,
        status_codes={200: 1},
        cache_headers={},
        useful_hits=1,
    )

    assert benchmark_summary_to_dict(summary)["url"] == "/x"
