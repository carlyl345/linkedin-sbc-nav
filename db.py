"""SQLite storage: schema, contact lookup, message merging/deduplication, stage rules, queries."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

STAGES = [
    "messaged",
    "replied",
    "chatting",
    "call_booked",
    "proposal_sent",
    "won",
    "lost",
    "not_a_fit",
]
AUTO_STAGES = ["messaged", "replied", "chatting"]  # the only stages import rules may set
CLOSED_STAGES = {"won", "lost", "not_a_fit"}
CHATTING_MIN_EACH_WAY = 3
DEDUPE_WINDOW = timedelta(hours=24)

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    profile_url     TEXT UNIQUE,
    role            TEXT,
    company         TEXT,
    stage           TEXT,
    opener_used     TEXT,
    first_contacted TEXT,
    last_outbound   TEXT,
    last_inbound    TEXT,
    next_follow_up  TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    sent_at     TEXT,
    content     TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('email', 'screenshot', 'manual')),
    is_preview  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id);

CREATE TABLE IF NOT EXISTS stage_history (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    from_stage  TEXT,
    to_stage    TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS processed_emails (
    gmail_id     TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    outcome      TEXT
);
"""


class AmbiguousContact(Exception):
    def __init__(self, query: str, candidates: list[dict]):
        super().__init__(f"'{query}' matches {len(candidates)} contacts")
        self.query = query
        self.candidates = candidates


class ContactNotFound(Exception):
    pass


@dataclass
class IncomingMessage:
    direction: str  # "in" | "out"
    content: str
    sent_at: datetime | None = None
    is_preview: bool = False


# ---------------------------------------------------------------- helpers

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_ELLIPSIS = re.compile(r"(…|\.\.\.)\s*$")


def normalize_text(text: str) -> str:
    text = _ELLIPSIS.sub("", text.strip())
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def normalize_profile_url(url: str) -> str:
    url = url.strip().split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"linkedin\.com/in/([^/]+)", url, re.I)
    return f"https://www.linkedin.com/in/{m.group(1).lower()}" if m else url


def looks_like_url(s: str) -> bool:
    return "linkedin.com/" in s.lower() or s.lower().startswith("http")


def same_text(a: str, b: str) -> bool:
    """Equal after normalising, or near-equal for longer texts (vision may misread a character)."""
    if a == b:
        return True
    if min(len(a), len(b)) < 25:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.95


def _times_close(a: datetime | None, b: datetime | None, window: timedelta = DEDUPE_WINDOW) -> bool:
    if a is None or b is None:
        return True
    return abs(a - b) <= window


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


# ---------------------------------------------------------------- database

class Tracker:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()  # MCP tools, Gmail poller and capture API share one file
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------ contacts

    def get_contact(self, contact_id: int) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if row is None:
            raise ContactNotFound(f"No contact with id {contact_id}")
        return dict(row)

    def find_contacts(self, query: str) -> list[dict]:
        query = query.strip()
        with self._conn() as c:
            if looks_like_url(query):
                rows = c.execute(
                    "SELECT * FROM contacts WHERE profile_url = ?", (normalize_profile_url(query),)
                ).fetchall()
                return [dict(r) for r in rows]
            rows = c.execute(
                "SELECT * FROM contacts WHERE name = ? COLLATE NOCASE", (query,)
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
            rows = c.execute(
                "SELECT * FROM contacts WHERE name LIKE ? COLLATE NOCASE ORDER BY name",
                (f"%{query}%",),
            ).fetchall()
            return [dict(r) for r in rows]

    def resolve_contact(self, query: str | int) -> dict:
        """One contact by id, name or profile URL; raises AmbiguousContact rather than guessing."""
        if isinstance(query, int) or (isinstance(query, str) and query.strip().isdigit()):
            return self.get_contact(int(query))
        matches = self.find_contacts(query)
        if not matches:
            raise ContactNotFound(f"No contact matches '{query}'")
        if len(matches) > 1:
            raise AmbiguousContact(query, [_brief(m) for m in matches])
        return matches[0]

    def create_contact(self, name: str, profile_url: str | None = None, role: str | None = None,
                       company: str | None = None) -> dict:
        ts = to_iso(now_utc())
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO contacts (name, profile_url, role, company, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name.strip(), normalize_profile_url(profile_url) if profile_url else None,
                 role, company, ts, ts),
            )
            new_id = cur.lastrowid
        return self.get_contact(new_id)

    def match_or_create(self, name: str, profile_url: str | None = None, role: str | None = None,
                        company: str | None = None) -> tuple[dict, bool]:
        """Used by intake. Exact match only (no partial-name guessing); returns (contact, created)."""
        with self._conn() as c:
            if profile_url:
                row = c.execute("SELECT * FROM contacts WHERE profile_url = ?",
                                (normalize_profile_url(profile_url),)).fetchone()
                if row:
                    return dict(row), False
            rows = c.execute("SELECT * FROM contacts WHERE name = ? COLLATE NOCASE",
                             (name.strip(),)).fetchall()
        if len(rows) > 1:
            raise AmbiguousContact(name, [_brief(dict(r)) for r in rows])
        if rows:
            contact = dict(rows[0])
            fill = {k: v for k, v in (("role", role), ("company", company)) if v and not contact.get(k)}
            if fill:
                self._update_fields(contact["id"], fill)
                contact = self.get_contact(contact["id"])
            return contact, False
        return self.create_contact(name, profile_url, role, company), True

    def _update_fields(self, contact_id: int, fields: dict) -> None:
        if not fields:
            return
        fields = dict(fields, updated_at=to_iso(now_utc()))
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE contacts SET {cols} WHERE id = ?", (*fields.values(), contact_id))

    def update_contact(self, contact_id: int, *, stage: str | None = None, role: str | None = None,
                       company: str | None = None, opener_used: str | None = None,
                       next_follow_up: str | None = None, notes: str | None = None,
                       profile_url: str | None = None, reason: str | None = None) -> dict:
        contact = self.get_contact(contact_id)
        fields: dict = {}
        for key, value in (("role", role), ("company", company), ("opener_used", opener_used),
                           ("notes", notes)):
            if value is not None:
                fields[key] = value
        if profile_url is not None:
            fields["profile_url"] = normalize_profile_url(profile_url) if profile_url else None
        if next_follow_up is not None:
            fields["next_follow_up"] = date.fromisoformat(next_follow_up).isoformat() if next_follow_up else None
        self._update_fields(contact_id, fields)
        if stage is not None and stage != contact["stage"]:
            self.set_stage(contact_id, stage, reason or "manual update")
        return self.get_contact(contact_id)

    def set_stage(self, contact_id: int, stage: str, reason: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"Unknown stage '{stage}'. Valid stages: {', '.join(STAGES)}")
        contact = self.get_contact(contact_id)
        if contact["stage"] == stage:
            return
        ts = to_iso(now_utc())
        with self._conn() as c:
            c.execute("UPDATE contacts SET stage = ?, updated_at = ? WHERE id = ?", (stage, ts, contact_id))
            c.execute(
                "INSERT INTO stage_history (contact_id, from_stage, to_stage, changed_at, reason)"
                " VALUES (?, ?, ?, ?, ?)",
                (contact_id, contact["stage"], stage, ts, reason),
            )

    # ------------------------------------------------------------ messages

    def get_messages(self, contact_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE contact_id = ?"
                " ORDER BY COALESCE(sent_at, created_at), id",
                (contact_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_messages(self, contact_id: int, incoming: Iterable[IncomingMessage], source: str) -> dict:
        """Merge messages into a contact's thread, skipping duplicates and upgrading email previews.

        Returns counts plus any automatic stage change.
        """
        added = replaced = duplicates = 0
        for msg in incoming:
            if not msg.content.strip():
                continue
            existing = self.get_messages(contact_id)
            outcome = self._merge_one(contact_id, msg, source, existing)
            added += outcome == "added"
            replaced += outcome == "replaced"
            duplicates += outcome == "duplicate"
        stage_change = self.refresh_contact(contact_id)
        return {"added": added, "replaced_previews": replaced, "duplicates": duplicates,
                "stage_change": stage_change}

    def _merge_one(self, contact_id: int, msg: IncomingMessage, source: str, existing: list[dict]) -> str:
        new_norm = normalize_text(msg.content)
        for ex in existing:
            if ex["direction"] != msg.direction:
                continue
            if not _times_close(from_iso(ex["sent_at"]), msg.sent_at):
                continue
            ex_norm = normalize_text(ex["content"])
            if same_text(ex_norm, new_norm):
                if ex["is_preview"] and not msg.is_preview:
                    self._replace_preview(ex, msg, source)
                    return "replaced"
                return "duplicate"
            # Email snippets are truncated: a preview is the start of the full message.
            if ex["is_preview"] and not msg.is_preview and new_norm.startswith(ex_norm) and ex_norm:
                self._replace_preview(ex, msg, source)
                return "replaced"
            if msg.is_preview and not ex["is_preview"] and ex_norm.startswith(new_norm) and new_norm:
                return "duplicate"
        with self._conn() as c:
            c.execute(
                "INSERT INTO messages (contact_id, direction, sent_at, content, source, is_preview, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (contact_id, msg.direction, to_iso(msg.sent_at), msg.content.strip(), source,
                 int(msg.is_preview), to_iso(now_utc())),
            )
        return "added"

    def _replace_preview(self, ex: dict, msg: IncomingMessage, source: str) -> None:
        # Keep the email's timestamp when present: it is exact, a screenshot's is often approximate.
        sent_at = ex["sent_at"] or to_iso(msg.sent_at)
        with self._conn() as c:
            c.execute(
                "UPDATE messages SET content = ?, source = ?, is_preview = 0, sent_at = ? WHERE id = ?",
                (msg.content.strip(), source, sent_at, ex["id"]),
            )

    def refresh_contact(self, contact_id: int) -> dict | None:
        """Recompute first/last timestamps and apply automatic stage rules. Returns the stage change."""
        msgs = self.get_messages(contact_id)
        outs = [m for m in msgs if m["direction"] == "out"]
        ins = [m for m in msgs if m["direction"] == "in"]

        def latest(ms):
            times = [m["sent_at"] or m["created_at"] for m in ms]
            return max(times, key=lambda t: from_iso(t)) if times else None

        def earliest(ms):
            times = [m["sent_at"] or m["created_at"] for m in ms]
            return min(times, key=lambda t: from_iso(t)) if times else None

        self._update_fields(contact_id, {
            "last_outbound": latest(outs),
            "last_inbound": latest(ins),
            "first_contacted": earliest(outs) or earliest(msgs),
        })

        target = auto_stage(len(ins), len(outs))
        current = self.get_contact(contact_id)["stage"]
        if target is None:
            return None
        if current is not None and current not in AUTO_STAGES:
            return None  # later stages are only ever set by hand or by Claude
        if current is not None and AUTO_STAGES.index(target) <= AUTO_STAGES.index(current):
            return None  # never move backwards automatically
        reason = f"auto: {len(ins)} inbound / {len(outs)} outbound messages"
        self.set_stage(contact_id, target, reason)
        return {"from": current, "to": target, "reason": reason}

    # ------------------------------------------------------------ email bookkeeping

    def email_processed(self, gmail_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM processed_emails WHERE gmail_id = ?", (gmail_id,)).fetchone() is not None

    def mark_email_processed(self, gmail_id: str, outcome: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO processed_emails (gmail_id, processed_at, outcome) VALUES (?, ?, ?)",
                      (gmail_id, to_iso(now_utc()), outcome))

    # ------------------------------------------------------------ queries

    def list_contacts(self, stage: str | None = None, needs_follow_up: bool | None = None,
                      follow_up_days: int = 4, today: date | None = None) -> list[dict]:
        if stage is not None and stage not in STAGES:
            raise ValueError(f"Unknown stage '{stage}'. Valid stages: {', '.join(STAGES)}")
        with self._conn() as c:
            if stage:
                rows = c.execute("SELECT * FROM contacts WHERE stage = ? ORDER BY name", (stage,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM contacts ORDER BY name").fetchall()
        contacts = [dict(r) for r in rows]
        if needs_follow_up is not None:
            due_ids = {d["id"] for d in self.follow_ups_due(today, follow_up_days)}
            contacts = [x for x in contacts if (x["id"] in due_ids) == needs_follow_up]
        keys = ("id", "name", "company", "stage", "last_inbound", "last_outbound", "next_follow_up")
        return [{k: x[k] for k in keys} for x in contacts]

    def follow_ups_due(self, today: date | None = None, follow_up_days: int = 4) -> list[dict]:
        today = today or now_utc().date()
        cutoff = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=follow_up_days)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM contacts").fetchall()
        due = []
        for r in map(dict, rows):
            if r["stage"] in CLOSED_STAGES:
                continue
            reasons = []
            if r["next_follow_up"] and date.fromisoformat(r["next_follow_up"]) <= today:
                reasons.append(f"next_follow_up was {r['next_follow_up']}")
            last_out, last_in = from_iso(r["last_outbound"]), from_iso(r["last_inbound"])
            if last_out and (last_in is None or last_out > last_in) and last_out < cutoff:
                days = (datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - last_out).days
                reasons.append(f"you sent the last message {days} days ago with no reply")
            if reasons:
                due.append({"id": r["id"], "name": r["name"], "company": r["company"], "stage": r["stage"],
                            "last_outbound": r["last_outbound"], "last_inbound": r["last_inbound"],
                            "next_follow_up": r["next_follow_up"], "why": "; ".join(reasons)})
        return sorted(due, key=lambda d: d["last_outbound"] or "")

    def opener_stats(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT COALESCE(c.opener_used, '(not set)') AS opener,
                       COUNT(*) AS contacts,
                       SUM(EXISTS (SELECT 1 FROM messages m WHERE m.contact_id = c.id AND m.direction = 'in')) AS replied
                FROM contacts c
                GROUP BY opener ORDER BY contacts DESC
                """
            ).fetchall()
        return [{"opener": r["opener"], "contacts": r["contacts"], "replied": r["replied"],
                 "reply_rate": round(r["replied"] / r["contacts"], 3) if r["contacts"] else 0.0} for r in rows]

    def pipeline_summary(self, days: int = 7) -> dict:
        since = to_iso(now_utc() - timedelta(days=days))
        with self._conn() as c:
            counts = {s: 0 for s in STAGES}
            counts["(none)"] = 0
            for r in c.execute("SELECT COALESCE(stage, '(none)') AS s, COUNT(*) AS n FROM contacts GROUP BY s"):
                counts[r["s"]] = r["n"]
            moves = c.execute(
                "SELECT h.changed_at, c.name, h.from_stage, h.to_stage, h.reason FROM stage_history h"
                " JOIN contacts c ON c.id = h.contact_id WHERE h.changed_at >= ? ORDER BY h.changed_at",
                (since,),
            ).fetchall()
            new_contacts = c.execute("SELECT COUNT(*) FROM contacts WHERE created_at >= ?", (since,)).fetchone()[0]
            msgs = c.execute(
                "SELECT direction, COUNT(*) AS n FROM messages WHERE COALESCE(sent_at, created_at) >= ? GROUP BY direction",
                (since,),
            ).fetchall()
        moved_into: dict[str, int] = {}
        for m in moves:
            moved_into[m["to_stage"]] = moved_into.get(m["to_stage"], 0) + 1
        return {
            "counts": {k: v for k, v in counts.items() if v or k in STAGES},
            "last_days": days,
            "new_contacts": new_contacts,
            "messages": {m["direction"]: m["n"] for m in msgs},
            "moved_into": moved_into,
            "moves": [dict(m) for m in moves],
        }


def auto_stage(inbound: int, outbound: int) -> str | None:
    if inbound >= CHATTING_MIN_EACH_WAY and outbound >= CHATTING_MIN_EACH_WAY:
        return "chatting"
    if inbound >= 1:
        return "replied"
    if outbound >= 1:
        return "messaged"
    return None


def _brief(c: dict) -> dict:
    return {k: c.get(k) for k in ("id", "name", "company", "role", "profile_url", "stage")}
