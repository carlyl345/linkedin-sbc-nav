from datetime import date, datetime, timedelta, timezone

import pytest

from db import AmbiguousContact, ContactNotFound, IncomingMessage as M, auto_stage

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def h(n):
    return T0 + timedelta(hours=n)


def test_auto_stage_rules():
    assert auto_stage(0, 0) is None
    assert auto_stage(0, 2) == "messaged"
    assert auto_stage(1, 1) == "replied"
    assert auto_stage(3, 2) == "replied"
    assert auto_stage(3, 3) == "chatting"


def test_stage_progression_and_history(tracker):
    c = tracker.create_contact("Sam Lee")
    r = tracker.add_messages(c["id"], [M("out", "Hi Sam", h(0))], "screenshot")
    assert r["stage_change"]["to"] == "messaged"
    tracker.add_messages(c["id"], [M("in", "Hey!", h(1))], "screenshot")
    assert tracker.get_contact(c["id"])["stage"] == "replied"
    tracker.add_messages(c["id"], [M("out", f"o{i}", h(2 + i)) for i in range(2)]
                         + [M("in", f"i{i}", h(5 + i)) for i in range(2)], "screenshot")
    assert tracker.get_contact(c["id"])["stage"] == "chatting"
    with tracker._conn() as conn:
        hist = [r["to_stage"] for r in conn.execute("SELECT to_stage FROM stage_history ORDER BY id")]
    assert hist == ["messaged", "replied", "chatting"]


def test_auto_rules_never_override_manual_later_stage(tracker):
    c = tracker.create_contact("Ana")
    tracker.update_contact(c["id"], stage="call_booked", reason="booked on call")
    tracker.add_messages(c["id"], [M("in", "see you then", h(0))], "email")
    assert tracker.get_contact(c["id"])["stage"] == "call_booked"


def test_overlapping_screenshots_dedupe(tracker):
    c = tracker.create_contact("Sam Lee")
    first = [M("out", "Hi Sam, loved your post on pricing", h(0)), M("in", "Thanks Carly!", h(1))]
    second = [M("in", "Thanks Carly!", h(1)), M("out", "Want to chat Thursday?", h(2))]
    tracker.add_messages(c["id"], first, "screenshot")
    r = tracker.add_messages(c["id"], second, "screenshot")
    assert (r["added"], r["duplicates"]) == (1, 1)
    assert len(tracker.get_messages(c["id"])) == 3


def test_dedupe_tolerates_small_vision_misreads_and_missing_times(tracker):
    c = tracker.create_contact("Sam Lee")
    tracker.add_messages(c["id"], [M("in", "We're mostly selling to mid-market finance teams right now.", h(0))], "screenshot")
    r = tracker.add_messages(c["id"], [M("in", "We’re mostly selling to mid-market finance teams right now", None)], "screenshot")
    assert r["duplicates"] == 1


def test_same_text_far_apart_is_not_duplicate(tracker):
    c = tracker.create_contact("Sam Lee")
    tracker.add_messages(c["id"], [M("in", "Thanks!", h(0))], "screenshot")
    r = tracker.add_messages(c["id"], [M("in", "Thanks!", h(72))], "screenshot")
    assert r["added"] == 1


def test_email_preview_replaced_by_screenshot(tracker):
    c = tracker.create_contact("Sam Lee")
    tracker.add_messages(c["id"], [M("in", "Hey Carly, thanks for reaching out. I'd love to hear more about…", h(0), is_preview=True)], "email")
    full = "Hey Carly, thanks for reaching out. I'd love to hear more about how you work with founders."
    r = tracker.add_messages(c["id"], [M("in", full, h(0) + timedelta(minutes=1))], "screenshot")
    assert r["replaced_previews"] == 1
    [m] = tracker.get_messages(c["id"])
    assert m["content"] == full and m["is_preview"] == 0 and m["source"] == "screenshot"
    assert m["sent_at"] == h(0).isoformat()  # email time kept


def test_email_preview_after_screenshot_is_duplicate(tracker):
    c = tracker.create_contact("Sam Lee")
    tracker.add_messages(c["id"], [M("in", "Sounds great, Thursday works for me. Send an invite?", h(0))], "screenshot")
    r = tracker.add_messages(c["id"], [M("in", "Sounds great, Thursday works…", h(0), is_preview=True)], "email")
    assert r["duplicates"] == 1


def test_lookup_by_name_url_and_ambiguity(tracker):
    a = tracker.create_contact("Sam Lee", profile_url="https://www.linkedin.com/in/SamLee/?trk=x")
    tracker.create_contact("Sam Patel")
    assert tracker.resolve_contact("https://linkedin.com/in/samlee")["id"] == a["id"]
    assert tracker.resolve_contact("sam lee")["id"] == a["id"]
    with pytest.raises(AmbiguousContact) as e:
        tracker.resolve_contact("Sam")
    assert {c["name"] for c in e.value.candidates} == {"Sam Lee", "Sam Patel"}
    with pytest.raises(ContactNotFound):
        tracker.resolve_contact("Nobody")


def test_follow_ups_due(tracker):
    today = date(2026, 11, 10)
    ghosted = tracker.create_contact("Ghost")
    tracker.add_messages(ghosted["id"], [M("out", "hi", datetime(2026, 11, 3, tzinfo=timezone.utc))], "manual")
    recent = tracker.create_contact("Recent")
    tracker.add_messages(recent["id"], [M("out", "hi", datetime(2026, 11, 9, tzinfo=timezone.utc))], "manual")
    replied = tracker.create_contact("Replied")
    tracker.add_messages(replied["id"], [M("out", "hi", datetime(2026, 11, 1, tzinfo=timezone.utc)),
                                         M("in", "hey", datetime(2026, 11, 2, tzinfo=timezone.utc))], "manual")
    scheduled = tracker.create_contact("Scheduled")
    tracker.update_contact(scheduled["id"], next_follow_up="2026-11-10")
    lost = tracker.create_contact("Lost")
    tracker.add_messages(lost["id"], [M("out", "hi", datetime(2026, 10, 1, tzinfo=timezone.utc))], "manual")
    tracker.update_contact(lost["id"], stage="lost")

    names = {d["name"] for d in tracker.follow_ups_due(today, 4)}
    assert names == {"Ghost", "Scheduled"}
    assert {c["name"] for c in tracker.list_contacts(needs_follow_up=True, today=today)} == names


def test_opener_stats_and_summary(tracker):
    a = tracker.create_contact("A"); b = tracker.create_contact("B"); c = tracker.create_contact("C")
    for x in (a, b):
        tracker.update_contact(x["id"], opener_used="post-comment")
    tracker.update_contact(c["id"], opener_used="cold")
    tracker.add_messages(a["id"], [M("out", "hi", h(0)), M("in", "yo", h(1))], "manual")
    tracker.add_messages(b["id"], [M("out", "hi", h(0))], "manual")
    stats = {s["opener"]: s for s in tracker.opener_stats()}
    assert stats["post-comment"]["reply_rate"] == 0.5
    assert stats["cold"]["reply_rate"] == 0.0
    summary = tracker.pipeline_summary(days=365 * 10)
    assert summary["counts"]["replied"] == 1 and summary["counts"]["messaged"] == 1
    assert summary["new_contacts"] == 3


def test_invalid_stage(tracker):
    c = tracker.create_contact("X")
    with pytest.raises(ValueError):
        tracker.update_contact(c["id"], stage="maybe")
