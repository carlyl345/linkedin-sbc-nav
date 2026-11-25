"""Gmail intake: OAuth (read-only), polling for LinkedIn message notifications, and parsing them.

The email format is NOT yet confirmed against real LinkedIn notifications. Everything that depends
on it lives in `parse_notification` and in the [gmail] section of config.toml, so it can be
corrected once real samples are available (drop them into tests/samples/emails/).
"""

from __future__ import annotations

import base64
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr

from config import Config, GmailConfig
from db import AmbiguousContact, IncomingMessage, Tracker

log = logging.getLogger("tracker.gmail")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class ParsedEmail:
    sender_name: str
    preview: str
    sent_at: datetime


# ---------------------------------------------------------------- parsing (pure, testable)

def _name_from_from_header(from_header: str, suffix: str) -> str | None:
    display, _addr = parseaddr(from_header)
    display = display.strip().strip('"')
    if suffix and display.lower().endswith(suffix.lower()):
        name = display[: -len(suffix)].strip()
        return name or None
    return None


def _name_from_subject(subject: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        m = re.search(pat, subject.strip(), re.I)
        if m and m.groupdict().get("name"):
            return m.group("name").strip().strip('"')
    return None


def _clean_preview(text: str, sender_name: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if sender_name and text.lower().startswith(sender_name.lower()):
        text = text[len(sender_name):].lstrip(" :-–—")
    return text


def parse_notification(headers: dict[str, str], snippet: str, internal_date_ms: int | str,
                       cfg: GmailConfig) -> ParsedEmail | None:
    """Turn one notification email into (sender, preview, time), or None if it doesn't look like one.

    Order of evidence for the sender: the From display name ("Sam Lee via LinkedIn"), then the
    subject patterns. Digests that bundle several people ("You have 3 new messages") return None.
    """
    subject = headers.get("subject", "")
    if re.search(r"\b\d+\s+new messages\b", subject, re.I):
        return None
    name = _name_from_from_header(headers.get("from", ""), cfg.from_name_suffix) or \
        _name_from_subject(subject, cfg.subject_patterns)
    if not name:
        return None
    preview = _clean_preview(snippet, name)
    if not preview:
        return None
    sent_at = datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc)
    return ParsedEmail(sender_name=name, preview=preview, sent_at=sent_at)


def headers_dict(payload: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def plain_text_body(payload: dict) -> str:
    """First text/plain part, decoded. Kept for when the real format needs more than the snippet."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = plain_text_body(part)
        if text:
            return text
    return ""


# ---------------------------------------------------------------- Gmail API

def gmail_service(cfg: GmailConfig, interactive: bool = False):
    """Build an authorised Gmail client. Only `interactive=True` (the login command) opens a browser."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if cfg.token_path.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.token_path), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        cfg.token_path.write_text(creds.to_json())
    if not creds or not creds.valid:
        if not interactive:
            raise RuntimeError("Gmail is not authorised yet. Run: python gmail_sync.py login")
        if not cfg.credentials_path.exists():
            raise RuntimeError(f"OAuth client file not found at {cfg.credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(cfg.credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)
        cfg.token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def sync_email(tracker: Tracker, config: Config, service=None, max_messages: int = 200,
               reprocess_unparsed: bool = False) -> dict:
    """Pull new notification emails into the tracker. Each Gmail message id is handled once."""
    cfg = config.gmail
    service = service or gmail_service(cfg)
    ids: list[str] = []
    page_token = None
    while len(ids) < max_messages:
        resp = service.users().messages().list(userId="me", q=cfg.query, pageToken=page_token,
                                               maxResults=min(100, max_messages)).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    summary = {"checked": len(ids), "logged": [], "duplicates": 0, "unparsed": [], "ambiguous": []}
    for gmail_id in reversed(ids):  # oldest first so stage rules see messages in order
        if tracker.email_processed(gmail_id) and not (reprocess_unparsed and _was_unparsed(tracker, gmail_id)):
            continue
        msg = service.users().messages().get(userId="me", id=gmail_id, format="full").execute()
        headers = headers_dict(msg.get("payload", {}))
        parsed = parse_notification(headers, msg.get("snippet", ""), msg.get("internalDate", 0), cfg)
        if parsed is None:
            summary["unparsed"].append({"gmail_id": gmail_id, "subject": headers.get("subject", "")})
            tracker.mark_email_processed(gmail_id, "unparsed")
            continue
        try:
            contact, created = tracker.match_or_create(parsed.sender_name)
        except AmbiguousContact as e:
            summary["ambiguous"].append({"gmail_id": gmail_id, "name": parsed.sender_name,
                                         "candidates": e.candidates})
            tracker.mark_email_processed(gmail_id, "unparsed")  # retried with reprocess_unparsed
            continue
        result = tracker.add_messages(
            contact["id"],
            [IncomingMessage("in", parsed.preview, parsed.sent_at, is_preview=True)],
            source="email",
        )
        tracker.mark_email_processed(gmail_id, "logged")
        if result["added"]:
            summary["logged"].append({"contact": contact["name"], "new_contact": created,
                                      "preview": parsed.preview[:120],
                                      "stage_change": result["stage_change"]})
        else:
            summary["duplicates"] += 1
    return summary


def _was_unparsed(tracker: Tracker, gmail_id: str) -> bool:
    with tracker._conn() as c:
        row = c.execute("SELECT outcome FROM processed_emails WHERE gmail_id = ?", (gmail_id,)).fetchone()
    return row is not None and row["outcome"] == "unparsed"


def dump_samples(config: Config, limit: int = 5) -> None:
    """Print headers + snippet of recent matching emails so the parser can be checked by eye."""
    service = gmail_service(config.gmail)
    resp = service.users().messages().list(userId="me", q=config.gmail.query, maxResults=limit).execute()
    for m in resp.get("messages", []):
        msg = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
        h = headers_dict(msg["payload"])
        print("From:   ", h.get("from"))
        print("Subject:", h.get("subject"))
        print("Snippet:", msg.get("snippet"))
        print("Parsed: ", parse_notification(h, msg.get("snippet", ""), msg.get("internalDate", 0), config.gmail))
        print("-" * 60)


if __name__ == "__main__":
    import json
    import sys

    from config import load_config

    conf = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "login":
        gmail_service(conf.gmail, interactive=True)
        print("Gmail authorised; token saved to", conf.gmail.token_path)
    elif cmd == "samples":
        dump_samples(conf)
    else:
        print(json.dumps(sync_email(Tracker(conf.db_path), conf), indent=2, default=str))
