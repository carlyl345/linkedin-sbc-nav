"""Localhost endpoint that receives screenshots from the Chrome extension and logs the thread."""

from __future__ import annotations

import base64
import hmac
import logging
from datetime import datetime, timezone
from typing import Callable

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import Config, load_config
from db import AmbiguousContact, IncomingMessage, Tracker
from vision import ThreadCapture, parse_timestamp, read_screenshot

log = logging.getLogger("tracker.capture")
MAX_IMAGE_BYTES = 15 * 1024 * 1024


class CaptureRequest(BaseModel):
    image: str  # data URL ("data:image/png;base64,...") or bare base64
    captured_at: str | None = None  # ISO 8601 with the browser's UTC offset
    contact: str | None = None  # optional override (id, name or profile URL) when a name is ambiguous


def decode_image(data: str) -> bytes:
    if data.startswith("data:"):
        header, _, data = data.partition(",")
        if "image/png" not in header:
            raise ValueError("Only PNG screenshots are accepted")
    raw = base64.b64decode(data, validate=True)
    if not raw.startswith(b"\x89PNG"):
        raise ValueError("Not a PNG image")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Screenshot is too large")
    return raw


def ingest_capture(tracker: Tracker, config: Config, png: bytes, captured_at: datetime,
                   contact_hint: str | None = None,
                   reader: Callable[..., ThreadCapture] = read_screenshot) -> dict:
    capture = reader(png, captured_at, config.my_name, model=config.vision_model,
                     api_key=config.anthropic_api_key())
    if not capture.is_linkedin_conversation or not capture.contact_name:
        return {"ok": False, "message": "That doesn't look like an open LinkedIn conversation."}

    if contact_hint:
        contact, created = tracker.resolve_contact(contact_hint), False
    else:
        contact, created = tracker.match_or_create(capture.contact_name, role=capture.role,
                                                   company=capture.company)
    incoming = [
        IncomingMessage("out" if m.sender == "me" else "in", m.text, parse_timestamp(m.timestamp))
        for m in capture.messages
    ]
    result = tracker.add_messages(contact["id"], incoming, source="screenshot")
    stage = tracker.get_contact(contact["id"])["stage"]
    new = result["added"] + result["replaced_previews"]
    first = contact["name"].split()[0]
    noun = "message" if new == 1 else "messages"
    text = f"Logged {new} new {noun} with {first}, stage: {stage or 'none'}"
    if created:
        text += " (new contact)"
    return {"ok": True, "message": text, "contact": contact["name"], "contact_id": contact["id"],
            "stage": stage, **result}


def create_app(config: Config | None = None, tracker: Tracker | None = None,
               reader: Callable[..., ThreadCapture] = read_screenshot) -> FastAPI:
    config = config or load_config()
    tracker = tracker or Tracker(config.db_path)
    app = FastAPI(title="LinkedIn tracker capture", docs_url=None, redoc_url=None, openapi_url=None)
    # Only the extension may call this from a browser context.
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
                       allow_methods=["POST", "GET"], allow_headers=["Content-Type", "X-Tracker-Secret"])

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/capture")
    def capture(req: CaptureRequest, x_tracker_secret: str = Header(default="")):
        if not config.shared_secret or config.shared_secret.startswith("change-me"):
            raise HTTPException(500, "Set shared_secret in config.toml first")
        if not hmac.compare_digest(x_tracker_secret.encode(), config.shared_secret.encode()):
            raise HTTPException(401, "Wrong shared secret")
        try:
            png = decode_image(req.image)
        except ValueError as e:
            raise HTTPException(400, str(e))
        captured_at = parse_timestamp(req.captured_at) or datetime.now(timezone.utc).astimezone()
        try:
            return ingest_capture(tracker, config, png, captured_at, req.contact, reader)
        except AmbiguousContact as e:
            names = ", ".join(f"{c['name']} (id {c['id']}, {c.get('company') or 'no company'})"
                              for c in e.candidates)
            return {"ok": False, "message": f"More than one contact is called {e.query}: {names}. "
                                            "Enter the id in the extension and capture again.",
                    "candidates": e.candidates}
        except Exception as e:  # surfaced in the extension popup
            log.exception("capture failed")
            raise HTTPException(502, f"Capture failed: {e}")

    return app


def serve(config: Config, tracker: Tracker | None = None) -> None:
    import uvicorn

    # Bind to loopback only; log to stderr so stdio MCP transport stays clean.
    uvicorn.run(create_app(config, tracker), host="127.0.0.1", port=config.capture_port,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    serve(load_config())
