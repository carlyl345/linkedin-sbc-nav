import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import vision
from capture_api import create_app
from vision import ThreadCapture, ThreadMessage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()
SCREENSHOTS = Path(__file__).parent / "samples" / "screenshots"


def fake_reader(captures):
    calls = iter(captures)

    def read(png, captured_at, my_name, **kw):
        assert png == PNG and my_name == "Carly"
        return next(calls)

    return read


def thread(*msgs, name="Sam Lee"):
    return ThreadCapture(is_linkedin_conversation=True, contact_name=name,
                         contact_headline="Founder at Acme", role="Founder", company="Acme",
                         messages=[ThreadMessage(sender=s, timestamp=t, text=x) for s, t, x in msgs])


def client_for(config, tracker, captures):
    return TestClient(create_app(config, tracker, reader=fake_reader(captures)))


def post(client, secret="s3cret", **extra):
    return client.post("/capture", headers={"X-Tracker-Secret": secret},
                       json={"image": DATA_URL, "captured_at": "2026-11-05T10:00:00-05:00", **extra})


def test_rejects_wrong_secret(config, tracker):
    r = post(client_for(config, tracker, []), secret="nope")
    assert r.status_code == 401


def test_rejects_non_png(config, tracker):
    c = client_for(config, tracker, [])
    r = c.post("/capture", headers={"X-Tracker-Secret": "s3cret"},
               json={"image": base64.b64encode(b"GIF89a").decode()})
    assert r.status_code == 400


def test_capture_logs_and_dedupes_second_click(config, tracker):
    t1 = thread(("me", "2026-11-04T09:00:00-05:00", "Hi Sam, loved your post"),
                ("them", "2026-11-04T11:00:00-05:00", "Thanks! What do you do?"))
    t2 = thread(("them", "2026-11-04T11:00:00-05:00", "Thanks! What do you do?"),
                ("me", "2026-11-04T11:30:00-05:00", "I help founders sell on LinkedIn"))
    c = client_for(config, tracker, [t1, t2])
    r1 = post(c).json()
    assert r1["ok"] and r1["message"] == "Logged 2 new messages with Sam, stage: replied (new contact)"
    r2 = post(c).json()
    assert r2["message"] == "Logged 1 new message with Sam, stage: replied"
    contact = tracker.resolve_contact("Sam Lee")
    assert contact["company"] == "Acme" and contact["role"] == "Founder"
    assert len(tracker.get_messages(contact["id"])) == 3


def test_not_a_conversation(config, tracker):
    cap = ThreadCapture(is_linkedin_conversation=False, contact_name=None, contact_headline=None,
                        role=None, company=None, messages=[])
    r = post(client_for(config, tracker, [cap])).json()
    assert r["ok"] is False


def test_ambiguous_name_then_override(config, tracker):
    a = tracker.create_contact("Sam Lee", company="Acme")
    tracker.create_contact("Sam Lee", profile_url="https://www.linkedin.com/in/sam-lee-2", company="Beta")
    t = thread(("them", None, "hello"))
    c = client_for(config, tracker, [t, t])
    r = post(c).json()
    assert r["ok"] is False and len(r["candidates"]) == 2
    r = post(c, contact=str(a["id"])).json()
    assert r["ok"] and r["contact_id"] == a["id"]


def test_vision_request_shape():
    class FakeMessages:
        def parse(self, **kw):
            self.kw = kw
            class R:
                stop_reason = "end_turn"
                parsed_output = thread(("me", None, "hi"))
            return R()

    class FakeClient:
        def __init__(self):
            self.beta = type("B", (), {"messages": FakeMessages()})()

    fc = FakeClient()
    out = vision.read_screenshot(PNG, datetime(2026, 11, 5, tzinfo=timezone.utc), "Carly", client=fc)
    kw = fc.beta.messages.kw
    assert out.messages[0].text == "hi"
    assert kw["output_format"] is ThreadCapture and kw["model"] == "claude-opus-5-5"
    assert kw["messages"][0]["content"][0]["source"]["media_type"] == "image/png"
    assert "Carly" in kw["system"]


# Live check against the real API on screenshots you provide: put foo.png next to foo.json
# ({"contact_name": "...", "messages": [{"sender": "me", "text": "..."}]}) and run
# RUN_VISION_TESTS=1 pytest tests/test_capture.py -k live
@pytest.mark.skipif(not os.environ.get("RUN_VISION_TESTS"), reason="calls the Claude API")
@pytest.mark.parametrize("png_path", sorted(SCREENSHOTS.glob("*.png")), ids=lambda p: p.stem)
def test_live_screenshot(png_path, config):
    expected = json.loads(png_path.with_suffix(".json").read_text())
    got = vision.read_screenshot(png_path.read_bytes(), datetime.now(timezone.utc).astimezone(),
                                 config.my_name, api_key=config.anthropic_api_key())
    assert got.contact_name == expected["contact_name"]
    assert [(m.sender, m.text) for m in got.messages] == \
        [(m["sender"], m["text"]) for m in expected["messages"]]
