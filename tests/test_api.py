"""The Runpod REST API v2 client.

Every test runs against `responses`, which intercepts at the HTTP adapter, so the real requests
code path runs — headers, query strings, status codes, streamed bodies — with nothing on the wire.
No live Runpod call is made anywhere in this file.

Request shapes are asserted against Runpod's own OpenAPI document, snapshot 2026-07-30. See
docs/adr/0003-api-operations.md.
"""

from __future__ import annotations

import pytest
import requests
import responses

from runpod_watchdog import api

KEY = "test-key-never-logged"
POD = "abc123"
POD_URL = f"https://api.runpod.io/v2/pods/{POD}"
ACTION_URL = f"{POD_URL}/action"
LOGS_URL = f"{POD_URL}/logs"

POD_BODY = {"id": POD, "status": "RUNNING", "actions": ["stop", "restart", "terminate"]}

TWO_EVENTS = (
    "id: 2026-06-01T12:02:03Z/000000000001\n"
    'data: {"ts":"2026-06-01T12:02:03Z","source":"container","line":"Model loaded."}\n'
    "\n"
    ": keep-alive\n"
    "\n"
    "id: 2026-06-01T12:02:04Z/000000000002\n"
    'data: {"ts":"2026-06-01T12:02:04Z","source":"system","line":"Started."}\n'
    "\n"
)


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    """Every test starts with a key in the environment. The ones about a missing key remove it."""
    monkeypatch.setenv(api.API_KEY_ENV, KEY)


@pytest.fixture
def mocked():
    with responses.RequestsMock() as rsps:
        yield rsps


@pytest.fixture
def client():
    with api.RunpodClient() as c:
        yield c


@pytest.fixture
def sent(monkeypatch):
    """Record the keyword arguments requests actually sends.

    `responses` patches the adapter below this point, so the timeout — a send() argument, not
    something carried on the prepared request — is only visible here.
    """
    calls: list[dict] = []
    original = requests.Session.send

    def spy(self, request, **kwargs):
        calls.append(kwargs)
        return original(self, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", spy)
    return calls


# --- request shape ------------------------------------------------------------------------------


def test_get_pod_asks_the_documented_address(client, mocked):
    mocked.add(responses.GET, POD_URL, json=POD_BODY, status=200)

    assert client.get_pod(POD) == POD_BODY

    request = mocked.calls[0].request
    assert request.method == "GET"
    assert request.url == POD_URL
    assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_the_base_url_does_not_repeat_the_version_segment(client, mocked):
    """The spec's `servers` entry is the bare host and every path key already starts with /v2.
    Appending /v2 to the base URL would produce /v2/v2/pods, which is the easy mistake here."""
    mocked.add(responses.GET, POD_URL, json=POD_BODY, status=200)

    client.get_pod(POD)

    assert "/v2/v2/" not in mocked.calls[0].request.url
    assert mocked.calls[0].request.url.startswith("https://api.runpod.io/v2/pods/")


def test_stop_pod_posts_the_documented_action(client, mocked):
    mocked.add(responses.POST, ACTION_URL, json=POD_BODY, status=200)

    assert client.stop_pod(POD) == POD_BODY

    request = mocked.calls[0].request
    assert request.method == "POST"
    assert request.url == ACTION_URL
    assert request.body == b'{"action": "stop"}'
    assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_terminate_pod_deletes_and_returns_nothing(client, mocked):
    """The spec's documented success for deletePod is 204 with no body."""
    mocked.add(responses.DELETE, POD_URL, status=204)

    assert client.terminate_pod(POD) is None

    assert mocked.calls[0].request.method == "DELETE"
    assert mocked.calls[0].request.url == POD_URL


def test_every_operation_sends_a_timeout(client, mocked, sent):
    """CLAUDE.md's rule: nothing may block forever. One test covers all four so a new operation
    that forgets a timeout cannot slip through."""
    mocked.add(responses.GET, POD_URL, json=POD_BODY, status=200)
    mocked.add(responses.POST, ACTION_URL, json=POD_BODY, status=200)
    mocked.add(responses.DELETE, POD_URL, status=204)
    mocked.add(responses.GET, LOGS_URL, body=TWO_EVENTS, status=200, content_type="text/event-stream")

    client.get_pod(POD)
    client.stop_pod(POD)
    client.terminate_pod(POD)
    list(client.stream_pod_logs(POD))

    assert len(sent) == 4
    for kwargs in sent:
        assert kwargs.get("timeout") is not None


# --- the API key --------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   "])
def test_a_missing_key_is_caught_before_anything_is_sent(client, mocked, monkeypatch, value):
    monkeypatch.setenv(api.API_KEY_ENV, value)

    with pytest.raises(api.MissingApiKeyError) as caught:
        client.get_pod(POD)

    assert api.API_KEY_ENV in str(caught.value)
    # Nothing left the machine. Counted rather than compared to [] — responses' CallList does not
    # compare equal to a plain list, even when it is empty.
    assert len(mocked.calls) == 0


def test_an_unset_key_is_caught_before_anything_is_sent(client, mocked, monkeypatch):
    monkeypatch.delenv(api.API_KEY_ENV, raising=False)

    with pytest.raises(api.MissingApiKeyError):
        client.get_pod(POD)

    assert len(mocked.calls) == 0


def test_the_key_is_read_fresh_on_every_call(client, mocked, monkeypatch):
    """The client is built before the key changes, and still uses the new one. Nothing long-lived
    holds a copy of the secret."""
    mocked.add(responses.GET, POD_URL, json=POD_BODY, status=200)
    monkeypatch.setenv(api.API_KEY_ENV, "a-different-key")

    client.get_pod(POD)

    assert mocked.calls[0].request.headers["Authorization"] == "Bearer a-different-key"


def test_the_key_never_appears_in_an_error_message(client, mocked):
    mocked.add(responses.GET, POD_URL, json={"title": "Unauthorized", "status": 401}, status=401)

    with pytest.raises(api.AuthError) as caught:
        client.get_pod(POD)

    assert KEY not in str(caught.value)


def test_a_key_echoed_back_by_runpod_is_redacted(client, mocked):
    """Defensive: if an error body ever quotes the key back, it does not get re-printed."""
    mocked.add(
        responses.GET,
        POD_URL,
        json={"title": "Unauthorized", "status": 401, "detail": f"bad key {KEY}"},
        status=401,
    )

    with pytest.raises(api.AuthError) as caught:
        client.get_pod(POD)

    assert KEY not in str(caught.value)
    assert "***" in str(caught.value)


# --- error mapping ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected", "must_say"),
    [
        (401, api.AuthError, api.API_KEY_ENV),
        (403, api.ForbiddenError, "not allowed"),
        (404, api.PodNotFoundError, "no pod"),
        (409, api.ConflictError, "current status"),
        (429, api.RateLimitedError, "rate limiting"),
        (500, api.ServerError, "server error"),
        (503, api.ServerError, "server error"),
        (418, api.UnexpectedStatusError, "unexpected"),
    ],
)
def test_each_http_failure_becomes_a_plain_sentence(client, mocked, status, expected, must_say):
    mocked.add(responses.GET, POD_URL, json={"title": "x", "status": status}, status=status)

    with pytest.raises(expected) as caught:
        client.get_pod(POD)

    assert must_say in str(caught.value)


def test_every_error_is_a_runpod_error(client, mocked):
    """One base class, so a caller can catch everything this module raises in one place."""
    mocked.add(responses.GET, POD_URL, status=404)

    with pytest.raises(api.RunpodError):
        client.get_pod(POD)


def test_the_detail_from_runpods_error_body_is_included(client, mocked):
    """The spec returns errors as application/problem+json with title, status, and detail."""
    mocked.add(
        responses.GET,
        POD_URL,
        json={"title": "Not Found", "status": 404, "detail": "pod not found"},
        status=404,
        content_type="application/problem+json",
    )

    with pytest.raises(api.PodNotFoundError) as caught:
        client.get_pod(POD)

    assert "pod not found" in str(caught.value)


def test_an_error_body_that_is_not_json_still_produces_a_message(client, mocked):
    mocked.add(responses.GET, POD_URL, body="<html>502 Bad Gateway</html>", status=502)

    with pytest.raises(api.ServerError) as caught:
        client.get_pod(POD)

    assert "HTTP 502" in str(caught.value)


def test_stopping_a_pod_in_the_wrong_status_is_a_conflict(client, mocked):
    """The spec returns 409 when the action is not valid for the pod's current status."""
    mocked.add(
        responses.POST,
        ACTION_URL,
        json={"title": "Conflict", "status": 409, "detail": "pod is already EXITED"},
        status=409,
    )

    with pytest.raises(api.ConflictError) as caught:
        client.stop_pod(POD)

    assert "pod is already EXITED" in str(caught.value)


def test_an_unreachable_runpod_becomes_a_network_error(client, mocked):
    mocked.add(responses.GET, POD_URL, body=requests.exceptions.ConnectionError("no route to host"))

    with pytest.raises(api.NetworkError) as caught:
        client.get_pod(POD)

    assert "Could not reach Runpod" in str(caught.value)


# --- logs ---------------------------------------------------------------------------------------


def test_log_events_are_parsed_from_the_stream(client, mocked):
    mocked.add(responses.GET, LOGS_URL, body=TWO_EVENTS, status=200, content_type="text/event-stream")

    events = list(client.stream_pod_logs(POD))

    assert events == [
        api.LogEvent(
            ts="2026-06-01T12:02:03Z",
            source="container",
            line="Model loaded.",
            event_id="2026-06-01T12:02:03Z/000000000001",
        ),
        api.LogEvent(
            ts="2026-06-01T12:02:04Z",
            source="system",
            line="Started.",
            event_id="2026-06-01T12:02:04Z/000000000002",
        ),
    ]


def test_a_malformed_event_is_skipped_not_raised(client, mocked):
    """A watchdog that crashed on one bad log line would leave the pod it was guarding running."""
    body = (
        "data: not json at all\n"
        "\n"
        'data: {"ts":"t","source":"container","line":"good"}\n'
        "\n"
    )
    mocked.add(responses.GET, LOGS_URL, body=body, status=200, content_type="text/event-stream")

    events = list(client.stream_pod_logs(POD))

    assert [e.line for e in events] == ["good"]


def test_log_query_parameters_are_sent(client, mocked):
    mocked.add(responses.GET, LOGS_URL, body="", status=200, content_type="text/event-stream")

    list(
        client.stream_pod_logs(
            POD, source="container", tail=50, since="2026-06-01T12:00:00Z", last_event_id="cursor-1"
        )
    )

    request = mocked.calls[0].request
    assert "source=container" in request.url
    assert "tail=50" in request.url
    assert "since=2026-06-01T12%3A00%3A00Z" in request.url
    assert request.headers["Last-Event-ID"] == "cursor-1"


def test_tail_zero_is_sent_rather_than_dropped(client, mocked):
    """0 means "no backfill, live only" in the spec. Treating it as unset would silently
    backfill 100 lines instead, which is the documented default."""
    mocked.add(responses.GET, LOGS_URL, body="", status=200, content_type="text/event-stream")

    list(client.stream_pod_logs(POD, tail=0))

    assert "tail=0" in mocked.calls[0].request.url


def test_no_query_string_when_no_options_are_given(client, mocked):
    mocked.add(responses.GET, LOGS_URL, body="", status=200, content_type="text/event-stream")

    list(client.stream_pod_logs(POD))

    assert mocked.calls[0].request.url == LOGS_URL


def test_read_pod_logs_collects_events(client, mocked):
    mocked.add(responses.GET, LOGS_URL, body=TWO_EVENTS, status=200, content_type="text/event-stream")

    events = client.read_pod_logs(POD, seconds=30)

    assert [e.line for e in events] == ["Model loaded.", "Started."]


def test_read_pod_logs_stops_at_the_deadline(client, mocked):
    """With no time budget the deadline has already passed, so collecting stops after the first
    event rather than draining the stream."""
    mocked.add(responses.GET, LOGS_URL, body=TWO_EVENTS, status=200, content_type="text/event-stream")

    events = client.read_pod_logs(POD, seconds=0)

    assert [e.line for e in events] == ["Model loaded."]


def test_read_pod_logs_passes_its_options_through(client, mocked):
    mocked.add(responses.GET, LOGS_URL, body=TWO_EVENTS, status=200, content_type="text/event-stream")

    client.read_pod_logs(POD, seconds=30, source="container", tail=10)

    assert "source=container" in mocked.calls[0].request.url
    assert "tail=10" in mocked.calls[0].request.url


def test_a_missing_pod_fails_the_log_stream_too(client, mocked):
    mocked.add(responses.GET, LOGS_URL, json={"title": "Not Found", "status": 404}, status=404)

    with pytest.raises(api.PodNotFoundError):
        list(client.stream_pod_logs(POD))


def test_a_silent_stream_raises_log_stream_idle(client, mocked, monkeypatch):
    """Mid-stream, a read timeout means the pod has gone quiet, which is not the same as the
    connection failing."""
    mocked.add(responses.GET, LOGS_URL, body="", status=200, content_type="text/event-stream")
    monkeypatch.setattr(
        requests.Response,
        "iter_lines",
        lambda self, *a, **k: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("quiet")),
    )

    with pytest.raises(api.LogStreamIdle):
        list(client.stream_pod_logs(POD))


def test_read_pod_logs_treats_silence_as_the_end_of_the_batch(client, mocked, monkeypatch):
    mocked.add(responses.GET, LOGS_URL, body="", status=200, content_type="text/event-stream")
    monkeypatch.setattr(
        requests.Response,
        "iter_lines",
        lambda self, *a, **k: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("quiet")),
    )

    assert client.read_pod_logs(POD, seconds=30) == []


def test_a_dropped_stream_becomes_a_network_error(client, mocked, monkeypatch):
    mocked.add(responses.GET, LOGS_URL, body="", status=200, content_type="text/event-stream")
    monkeypatch.setattr(
        requests.Response,
        "iter_lines",
        lambda self, *a, **k: (_ for _ in ()).throw(
            requests.exceptions.ChunkedEncodingError("dropped")
        ),
    )

    with pytest.raises(api.NetworkError):
        list(client.stream_pod_logs(POD))


# --- the Server-Sent Events parser ----------------------------------------------------------------


def test_blank_lines_separate_events():
    lines = [b"data: one", b"", b"data: two", b""]

    assert list(api._iter_sse_events(lines)) == [(None, "one"), (None, "two")]


def test_comment_lines_are_keep_alives_and_are_ignored():
    lines = [b": ping", b"", b"data: one", b""]

    assert list(api._iter_sse_events(lines)) == [(None, "one")]


def test_repeated_data_fields_join_with_newlines():
    """The Server-Sent Events format allows a payload to span several data: lines."""
    lines = [b"data: first", b"data: second", b""]

    assert list(api._iter_sse_events(lines)) == [(None, "first\nsecond")]


def test_a_final_event_without_a_trailing_blank_line_is_still_yielded():
    lines = [b"id: 7", b"data: one"]

    assert list(api._iter_sse_events(lines)) == [("7", "one")]


def test_only_one_leading_space_is_stripped_from_a_value():
    """Per the Server-Sent Events format a single space after the colon is separator, not data —
    so a log line that starts with an indent keeps it."""
    lines = [b"data:  indented", b""]

    assert list(api._iter_sse_events(lines)) == [(None, " indented")]
