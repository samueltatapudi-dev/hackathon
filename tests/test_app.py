from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as app_module


@pytest.fixture
def client(monkeypatch, tmp_path):
    events: list[dict] = []

    def capture_event(event):
        events.append(event)

    monkeypatch.setattr(app_module, "append_event", capture_event)
    monkeypatch.setattr(app_module, "EVENTS_FILE", str(tmp_path / "events.json"))

    app_module.announcements.clear()
    app_module.next_announcement_id = 1
    app_module.app.config.update(TESTING=True)

    with app_module.app.test_client() as test_client:
        yield test_client, events

    app_module.announcements.clear()
    app_module.next_announcement_id = 1


def _create_sample_announcement(client):
    payload = {
        "title": "Security Update",
        "details": "Review the new policy before acknowledging.",
        "target": "https://example.com/policy",
    }
    response = client.post("/announcement", json=payload)
    assert response.status_code == 201
    return payload, response.get_json()


def test_create_announcement_returns_tracking_link(client):
    test_client, events = client
    payload, data = _create_sample_announcement(test_client)

    announcement = data["announcement"]
    track_link = data["track"]

    assert announcement["id"] == 1
    assert announcement["target"] == payload["target"]

    parsed = urlparse(track_link)
    assert parsed.path.endswith("/track")
    params = parse_qs(parsed.query)
    assert params["id"] == ["1"]
    assert params["target"] == [payload["target"]]
    assert events == []  # No tracking event until the link is opened.


def test_track_open_records_event_and_contains_task_link(client):
    test_client, events = client
    payload, _ = _create_sample_announcement(test_client)

    response = test_client.get(
        "/track",
        query_string={
            "id": 1,
            "target": payload["target"],
            "user": "employee@example.com",
        },
    )

    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "Start Task" in body
    assert f'href="{payload["target"]}"' in body
    assert 'value="employee@example.com"' in body

    assert len(events) == 1
    opened = events[0]
    assert opened["event"] == "opened"
    assert opened["announcementId"] == 1
    assert opened["target"] == payload["target"]
    assert opened["user"] == "employee@example.com"


def test_acknowledge_records_event(client):
    test_client, events = client
    payload, _ = _create_sample_announcement(test_client)

    # Visiting the track page is expected before acknowledgement.
    test_client.get(
        "/track",
        query_string={"id": 1, "target": payload["target"], "user": "employee@example.com"},
    )

    response = test_client.post(
        "/acknowledge",
        data={
            "announcementId": "1",
            "user": "employee@example.com",
            "target": payload["target"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert len(events) == 2
    ack = events[-1]
    assert ack["event"] == "acknowledged"
    assert ack["announcementId"] == 1
    assert ack["target"] == payload["target"]
    assert ack["user"] == "employee@example.com"


def test_proceed_redirects_to_track(client):
    test_client, events = client
    payload, _ = _create_sample_announcement(test_client)

    response = test_client.get(
        "/proceed",
        query_string={"id": "1", "target": payload["target"], "user": "employee@example.com"},
    )

    assert response.status_code == 302
    location = response.headers["Location"]
    parsed = urlparse(location)
    assert parsed.path == "/track"
    params = parse_qs(parsed.query)
    assert params["id"] == ["1"]
    assert params["target"] == [payload["target"]]
    assert len(events) == 0
