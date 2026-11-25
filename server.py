"""MCP server (stdio) exposing the LinkedIn sales tracker to Claude.

Starting it also starts the localhost capture endpoint and the Gmail poller in background threads,
so Claude Desktop launching this one process is enough.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import date
from functools import wraps
from typing import Literal

from mcp.server.mcpserver import MCPServer

import gmail_sync
from config import load_config
from db import STAGES, AmbiguousContact, ContactNotFound, IncomingMessage, Tracker

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("tracker")

config = load_config()
tracker = Tracker(config.db_path)
_gmail_lock = threading.Lock()

mcp = MCPServer(
    "linkedin-sales-tracker",
    instructions=(
        "Tracks Carly's LinkedIn selling-by-chat pipeline. One record per person with a stage: "
        + ", ".join(STAGES) + ". Contacts can be referred to by id, name or LinkedIn profile URL. "
        "When a lookup returns 'candidates', ask which person is meant (or use the id) instead of guessing. "
        "Read get_conversation before suggesting a stage change, and give a reason when you change one."
    ),
)


def _friendly_errors(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AmbiguousContact as e:
            return {"error": f"'{e.query}' matches more than one contact; pass the id instead.",
                    "candidates": e.candidates}
        except (ContactNotFound, ValueError) as e:
            return {"error": str(e)}
    return wrapper


@mcp.tool()
@_friendly_errors
def sync_email() -> dict:
    """Pull new LinkedIn message notification emails from Gmail now and return what changed."""
    if not config.gmail.enabled:
        return {"error": "Gmail sync is disabled in config.toml"}
    try:
        with _gmail_lock:
            return gmail_sync.sync_email(tracker, config)
    except RuntimeError as e:
        return {"error": str(e)}


@mcp.tool()
@_friendly_errors
def list_contacts(stage: Literal[tuple(STAGES)] | None = None,  # type: ignore[valid-type]
                  needs_follow_up: bool | None = None) -> list[dict]:
    """List contacts with name, company, stage, last_inbound, last_outbound and next_follow_up.

    Filter by stage and/or by whether a follow-up is due (see follow_ups_due for the rule).
    """
    return tracker.list_contacts(stage, needs_follow_up, config.follow_up_days)


@mcp.tool()
@_friendly_errors
def get_conversation(contact: str) -> dict:
    """Return a contact's record and stored message thread (oldest first) so you can judge the stage.

    `contact` is an id, a name or a LinkedIn profile URL. Messages with is_preview=true are email
    snippets and may be truncated; a screenshot capture replaces them with the full text.
    """
    c = tracker.resolve_contact(contact)
    msgs = [{"direction": m["direction"], "sent_at": m["sent_at"], "text": m["content"],
             "source": m["source"], "is_preview": bool(m["is_preview"])}
            for m in tracker.get_messages(c["id"])]
    return {"contact": c, "messages": msgs}


@mcp.tool()
@_friendly_errors
def update_contact(contact: str,
                   stage: Literal[tuple(STAGES)] | None = None,  # type: ignore[valid-type]
                   role: str | None = None, company: str | None = None,
                   opener_used: str | None = None, next_follow_up: str | None = None,
                   notes: str | None = None, profile_url: str | None = None,
                   reason: str | None = None) -> dict:
    """Update a contact's fields. Stage changes are logged to stage_history with `reason`.

    next_follow_up is a date (YYYY-MM-DD); pass an empty string to clear it. `notes` replaces the
    existing notes, so include anything worth keeping.
    """
    c = tracker.resolve_contact(contact)
    return tracker.update_contact(c["id"], stage=stage, role=role, company=company,
                                  opener_used=opener_used, next_follow_up=next_follow_up,
                                  notes=notes, profile_url=profile_url, reason=reason)


@mcp.tool()
@_friendly_errors
def log_message(contact: str, direction: Literal["in", "out"], text: str,
                sent_at: str | None = None) -> dict:
    """Manually log a message that intake missed. direction is 'out' for Carly's, 'in' for theirs.

    Creates the contact if no one by that name exists yet. sent_at is optional ISO 8601.
    """
    from vision import parse_timestamp

    try:
        c = tracker.resolve_contact(contact)
        created = False
    except ContactNotFound:
        if contact.strip().isdigit() or "linkedin.com" in contact.lower():
            raise  # can't create a contact without a name
        c, created = tracker.create_contact(contact), True
    result = tracker.add_messages(c["id"], [IncomingMessage(direction, text, parse_timestamp(sent_at))],
                                  source="manual")
    return {"contact": c["name"], "contact_id": c["id"], "new_contact": created, **result}


@mcp.tool()
@_friendly_errors
def follow_ups_due(today: str | None = None) -> list[dict]:
    """Contacts needing a follow-up: next_follow_up has passed, or Carly sent the last message more
    than follow_up_days (default 4) ago with no reply. Closed stages are excluded. today: YYYY-MM-DD."""
    return tracker.follow_ups_due(date.fromisoformat(today) if today else None, config.follow_up_days)


@mcp.tool()
@_friendly_errors
def opener_stats() -> list[dict]:
    """Reply rate per opener_used (share of contacts with at least one inbound message)."""
    return tracker.opener_stats()


@mcp.tool()
@_friendly_errors
def pipeline_summary() -> dict:
    """Counts per stage, plus the last 7 days' stage movement, new contacts and message volume."""
    return tracker.pipeline_summary()


# ---------------------------------------------------------------- background services

def _start_capture_api() -> None:
    import capture_api

    def run():
        try:
            capture_api.serve(config, tracker)
        except BaseException:
            log.exception("Capture endpoint stopped")

    threading.Thread(target=run, name="capture-api", daemon=True).start()
    log.info("Capture endpoint on http://127.0.0.1:%s/capture", config.capture_port)


def _start_gmail_poller() -> None:
    if not config.gmail.enabled:
        return

    def loop():
        while True:
            try:
                with _gmail_lock:
                    result = gmail_sync.sync_email(tracker, config)
                if result["logged"]:
                    log.info("Gmail: logged %d new messages", len(result["logged"]))
            except Exception as e:
                log.warning("Gmail poll skipped: %s", e)
            time.sleep(config.gmail.poll_minutes * 60)

    threading.Thread(target=loop, name="gmail-poller", daemon=True).start()


def main() -> None:
    _start_capture_api()
    _start_gmail_poller()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
