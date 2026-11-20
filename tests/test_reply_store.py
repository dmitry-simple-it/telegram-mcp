import stat

import pytest

from telegram_mcp.reply_store import ReplyStore


def make_sub(store, **kw):
    values = dict(
        account="default",
        chat_id=42,
        after_message_id=10,
        client="codex",
        session_id="thread-1",
        endpoint="/tmp/fake-socket",
        now=100,
        settle_seconds=6,
    )
    values.update(kw)
    return store.subscribe(**values)


def observe(store, message_id=11, **kw):
    values = dict(
        account="default",
        chat_id=42,
        message_id=message_id,
        sender_id=42,
        topic_id=None,
        reply_to=None,
        now=101,
    )
    values.update(kw)
    return store.observe(**values)


def test_two_consumers_receive_same_event_and_restart_keeps_pending(tmp_path):
    db = tmp_path / "state" / "replies.db"
    store = ReplyStore(db)
    a = make_sub(store)
    b = make_sub(store, session_id="thread-2")
    observe(store)
    batches = store.prepare(now=108)
    assert {b["subscription_id"] for b in batches} == {a["id"], b["id"]}
    assert all(b["message_ids"] == [11] for b in batches)
    reopened = ReplyStore(db)
    assert {b["id"] for b in reopened.prepare(now=109)} == {b["id"] for b in batches}
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


def test_filters_account_sender_topic_and_anchor(tmp_path):
    store = ReplyStore(tmp_path / "db")
    sub = make_sub(store, chat_id=-100, sender_id=7, topic_id=55)
    for mid, change in [
        (9, {}),
        (11, {"account": "other"}),
        (12, {"chat_id": -200}),
        (13, {"sender_id": 8}),
        (14, {"topic_id": 56}),
        (15, {}),
    ]:
        observe(store, mid, **dict(dict(chat_id=-100, sender_id=7, topic_id=55), **change))
    (batch,) = store.prepare(now=110)
    assert batch["subscription_id"] == sub["id"]
    assert batch["message_ids"] == [15]  # ordinary message with no reply_to is valid


def test_registration_after_reply_duplicate_ingest_and_out_of_order(tmp_path):
    store = ReplyStore(tmp_path / "db")
    observe(store, 12)
    observe(store, 12)
    sub = make_sub(store, now=104)
    (batch,) = store.prepare(now=108)
    assert batch["message_ids"] == [12]
    store.acknowledge(batch["id"], "thread-1", now=109)
    observe(store, 11, now=110)  # late lower message ID must not be lost
    (batch2,) = store.prepare(now=117)
    assert batch2["message_ids"] == [11]
    assert store.list_subscriptions(session_id="thread-1")[0]["id"] == sub["id"]


def test_debounce_and_pending_batch_do_not_steal_new_messages(tmp_path):
    store = ReplyStore(tmp_path / "db")
    make_sub(store)
    observe(store)
    observe(store, 12, now=105)
    assert store.prepare(now=110) == []
    (batch,) = store.prepare(now=112)
    assert batch["message_ids"] == [11, 12]
    observe(store, 13, now=113)
    assert [b["id"] for b in store.prepare(now=120)] == [batch["id"]]
    store.acknowledge(batch["id"], "thread-1", now=121)
    (next_batch,) = store.prepare(now=122)
    assert next_batch["message_ids"] == [13]


def test_ack_is_target_scoped_idempotent_and_first_reply_closes(tmp_path):
    store = ReplyStore(tmp_path / "db")
    make_sub(store, mode="first_reply")
    observe(store)
    (batch,) = store.prepare(now=108)
    with pytest.raises(ValueError, match="session"):
        store.acknowledge(batch["id"], "wrong-thread", now=109)
    assert store.acknowledge(batch["id"], "thread-1", now=109)["status"] == "acked"
    assert store.acknowledge(batch["id"], "thread-1", now=110)["status"] == "acked"
    assert store.list_subscriptions(session_id="thread-1")[0]["status"] == "completed"
    assert store.prepare(now=120) == []


def test_expiry_cancellation_validation_and_idempotent_subscribe(tmp_path):
    store = ReplyStore(tmp_path / "db")
    sub = make_sub(store, ttl_seconds=10)
    assert make_sub(store, ttl_seconds=10)["id"] == sub["id"]
    observe(store)
    assert store.prepare(now=111) == []
    sub2 = make_sub(store, session_id="thread-2")
    with pytest.raises(ValueError, match="session"):
        store.cancel(sub2["id"], "wrong")
    store.cancel(sub2["id"], "thread-2")
    assert store.prepare(now=120) == []
    for changes in [
        {"ttl_seconds": 0},
        {"ttl_seconds": 604801},
        {"client": "shell"},
        {"settle_seconds": -1},
        {"session_id": ""},
        {"after_message_id": -1},
    ]:
        with pytest.raises(ValueError):
            make_sub(store, **changes)


def test_inflight_delivery_cannot_be_claimed_twice_and_survives_restart(tmp_path):
    store = ReplyStore(tmp_path / "db")
    make_sub(store)
    observe(store)
    (batch,) = store.prepare(now=108)
    assert store.claim(batch["id"], now=108)
    assert not store.claim(batch["id"], now=109)
    reopened = ReplyStore(tmp_path / "db")
    assert reopened.prepare(now=300) == []  # uncertain send is NOT blindly retried
    assert reopened.deliveries(status="sending")[0]["id"] == batch["id"]
    reopened.mark_sent(batch["id"], now=301)
    assert reopened.deliveries(status="sent")[0]["id"] == batch["id"]
    reopened.acknowledge(batch["id"], "thread-1", now=302)
    assert reopened.prepare(now=310) == []


def test_pre_send_failure_is_retryable_but_record_is_bounded(tmp_path):
    store = ReplyStore(tmp_path / "db")
    make_sub(store)
    observe(store)
    (batch,) = store.prepare(now=108)
    store.claim(batch["id"], now=108)
    store.fail(batch["id"], "offline " * 300, retryable=True, now=109)
    assert store.prepare(now=110) == []  # exponential backoff
    (again,) = store.prepare(now=180)
    assert again["id"] == batch["id"]
    assert len(again["last_error"]) <= 500


def test_expired_sent_batches_are_not_replayed_and_old_metadata_is_pruned(tmp_path):
    store = ReplyStore(tmp_path / "db")
    make_sub(store, client="claude", endpoint="", ttl_seconds=10)
    observe(store)
    (item,) = store.prepare(now=108)
    store.claim(item["id"], now=108)
    store.mark_sent(item["id"], now=109)
    store.prepare(now=111)
    assert store.deliveries(status="sent") == []
    store.prune(now=100 + 8 * 86400)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0


def test_rebinding_same_thread_updates_restarted_socket_without_duplicate_subscription(tmp_path):
    store = ReplyStore(tmp_path / "db")
    original = make_sub(store)
    rebound = make_sub(store, endpoint="/tmp/new-private-socket")
    assert rebound["id"] == original["id"]
    assert rebound["endpoint"] == "/tmp/new-private-socket"
