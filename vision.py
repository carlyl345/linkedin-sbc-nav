"""Turns a screenshot of a LinkedIn conversation into structured messages with the Claude API."""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger("tracker.vision")


class ThreadMessage(BaseModel):
    sender: Literal["me", "them"]
    timestamp: str | None = Field(
        description="ISO 8601 with UTC offset, resolved against the capture time; null if not visible")
    text: str


class ThreadCapture(BaseModel):
    is_linkedin_conversation: bool
    contact_name: str | None
    contact_headline: str | None
    role: str | None = Field(description="Job title taken from the headline, if it names one")
    company: str | None = Field(description="Company taken from the headline, if it names one")
    messages: list[ThreadMessage]


SYSTEM = """You transcribe screenshots of LinkedIn messaging conversations into JSON.

Rules:
- The user of the screenshot is {my_name}. Messages written by {my_name} have sender "me"; messages \
from the other person have sender "them". LinkedIn shows the author's name and photo above a group \
of messages; every message in that group has the same author.
- contact_name is the other person in the open conversation (shown in the conversation header), \
not someone in the conversation list on the left.
- Copy each message's text exactly, including emoji and line breaks. Do not summarise. Skip \
system lines such as "You accepted the invite", reactions, read receipts and "Seen".
- Only include messages whose text is fully visible. Leave out a message cut off at the top or \
bottom edge of the screenshot.
- Timestamps: LinkedIn shows a time per group (e.g. "10:32 AM") under day separators ("TODAY", \
"YESTERDAY", "MONDAY", "MAR 3"). Resolve them to absolute ISO 8601 times using the capture time \
given below and its UTC offset. Give every message in a group its group's time. Use null when \
no time can be worked out.
- If the screenshot is not an open LinkedIn conversation, set is_linkedin_conversation to false \
and return no messages."""


def _client(api_key: str | None) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def read_screenshot(png_bytes: bytes, captured_at: datetime, my_name: str, *,
                    model: str = "claude-opus-5-5", api_key: str | None = None,
                    client: anthropic.Anthropic | None = None) -> ThreadCapture:
    client = client or _client(api_key)
    image_b64 = base64.standard_b64encode(png_bytes).decode()
    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # retried on another model if a safety classifier declines
        output_config={"effort": "medium"},
        system=SYSTEM.format(my_name=my_name),
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": f"Capture time: {captured_at.isoformat()}"},
            ],
        }],
        output_format=ThreadCapture,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The screenshot could not be read (the model declined).")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The screenshot transcription was cut off; try a shorter part of the thread.")
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError("The model returned no structured result for this screenshot.")
    return parsed


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("Unparseable timestamp from vision: %r", value)
        return None
