"""Parser tests on SYNTHETIC samples. Replace/extend with real emails in tests/samples/emails/
once the real LinkedIn notification format has been confirmed."""

import json
from pathlib import Path

import pytest

import gmail_sync
from gmail_sync import parse_notification

SAMPLES = Path(__file__).parent / "samples" / "emails"


def load(name):
    return json.loads((SAMPLES / name).read_text())


@pytest.mark.parametrize("path", sorted(SAMPLES.glob("*.json")), ids=lambda p: p.stem)
def test_samples(path, config):
    s = json.loads(path.read_text())
    parsed = parse_notification({k.lower(): v for k, v in s["headers"].items()}, s["snippet"],
                                s["internalDate"], config.gmail)
    expected = s["expected"]
    if expected is None:
        assert parsed is None
    else:
        assert parsed.sender_name == expected["sender_name"]
        assert parsed.preview.startswith(expected["preview_starts_with"])


class FakeGmail:
    """Minimal stand-in for the googleapiclient Gmail service."""

    def __init__(self, messages):
        self.messages_by_id = {m["id"]: m for m in messages}
        self.order = [m["id"] for m in messages]

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, pageToken=None, maxResults=100):
        return _Exec({"messages": [{"id": i} for i in reversed(self.order)]})

    def get(self, userId, id, format):
        return _Exec(self.messages_by_id[id])


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


def _gmail_msg(gid, sample):
    return {"id": gid, "snippet": sample["snippet"], "internalDate": sample["internalDate"],
            "payload": {"headers": [{"name": k, "value": v} for k, v in sample["headers"].items()]}}


def test_sync_logs_once_and_reports_unparsed(tracker, config):
    msgs = [_gmail_msg("g1", load("from_display_name.json")),
            _gmail_msg("g2", load("digest_multiple.json"))]
    service = FakeGmail(msgs)
    first = gmail_sync.sync_email(tracker, config, service=service)
    assert [l["contact"] for l in first["logged"]] == ["Sam Lee"]
    assert first["logged"][0]["stage_change"]["to"] == "replied"
    assert [u["gmail_id"] for u in first["unparsed"]] == ["g2"]
    second = gmail_sync.sync_email(tracker, config, service=service)
    assert second["logged"] == [] and second["unparsed"] == []
    [m] = tracker.get_messages(tracker.resolve_contact("Sam Lee")["id"])
    assert m["is_preview"] == 1 and m["source"] == "email"
