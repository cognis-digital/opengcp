"""Extended Pub/Sub tests: ordering keys, dead-letter policy,
modify_ack_deadline, push delivery."""

import time

import pytest

from opengcp.pubsub import (PubSub, PubSubError, TopicNotFound,
                            SubscriptionNotFound, DeadLetterPolicy)


# ----- ordering keys -----

def test_ordering_key_delivers_in_order():
    """Messages with the same ordering key must not be delivered simultaneously."""
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ps.publish("t", b"msg1", ordering_key="grp")
    ps.publish("t", b"msg2", ordering_key="grp")
    ps.publish("t", b"msg3", ordering_key="grp")

    # First pull: only msg1 comes out (key is blocked)
    received = ps.pull("s", max_messages=10)
    assert len(received) == 1
    import base64
    assert base64.b64decode(received[0]["message"]["data"]) == b"msg1"

    # msg2 and msg3 are still blocked
    assert ps.pull("s") == []

    # Ack msg1 -> msg2 becomes available
    ps.ack("s", [received[0]["ackId"]])
    received2 = ps.pull("s", max_messages=10)
    assert len(received2) == 1
    assert base64.b64decode(received2[0]["message"]["data"]) == b"msg2"

    ps.ack("s", [received2[0]["ackId"]])
    received3 = ps.pull("s")
    assert len(received3) == 1
    assert base64.b64decode(received3[0]["message"]["data"]) == b"msg3"


def test_ordering_key_nack_requeues_correctly():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ps.publish("t", b"a", ordering_key="k")
    ps.publish("t", b"b", ordering_key="k")

    received = ps.pull("s")
    assert len(received) == 1
    ps.nack("s", [received[0]["ackId"]])

    # a is re-enqueued at the front; key is unblocked
    again = ps.pull("s")
    assert len(again) == 1
    import base64
    assert base64.b64decode(again[0]["message"]["data"]) == b"a"


def test_different_ordering_keys_are_independent():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ps.publish("t", b"a1", ordering_key="a")
    ps.publish("t", b"b1", ordering_key="b")
    ps.publish("t", b"a2", ordering_key="a")
    ps.publish("t", b"b2", ordering_key="b")

    # Should get one from each distinct key
    received = ps.pull("s", max_messages=10)
    assert len(received) == 2
    keys = {r["message"]["orderingKey"] for r in received}
    assert keys == {"a", "b"}


def test_no_ordering_key_all_available():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    for i in range(5):
        ps.publish("t", f"msg{i}".encode())
    received = ps.pull("s", max_messages=10)
    assert len(received) == 5


# ----- dead-letter policy -----

def test_dead_letter_on_nack_exceeds_attempts():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_topic("dl")
    ps.create_subscription("dl-sub", "dl")
    dl_policy = DeadLetterPolicy(dead_letter_topic="dl", max_delivery_attempts=2)
    ps.create_subscription("s", "t", dead_letter_policy=dl_policy)

    ps.publish("t", b"poison")

    # Attempt 1: pull + nack
    msgs = ps.pull("s")
    assert len(msgs) == 1
    ps.nack("s", [msgs[0]["ackId"]])  # delivery_attempt becomes 2

    # Attempt 2: pull + nack -> exceeds max (2), forwarded to DL
    msgs = ps.pull("s")
    assert len(msgs) == 1
    ps.nack("s", [msgs[0]["ackId"]])  # delivery_attempt becomes 3 > 2

    # Dead-letter sub should have received the message
    dl_msgs = ps.pull("dl-sub")
    assert len(dl_msgs) == 1
    dl_attrs = dl_msgs[0]["message"]["attributes"]
    assert "original_message_id" in dl_attrs
    assert dl_attrs["original_subscription"] == "s"


def test_dead_letter_on_expired_deadline():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_topic("dl")
    ps.create_subscription("dl-sub", "dl")
    dl_policy = DeadLetterPolicy(dead_letter_topic="dl", max_delivery_attempts=2)
    ps.create_subscription("s", "t", ack_deadline=0.1, dead_letter_policy=dl_policy)

    ps.publish("t", b"x")

    # Pull (attempt 1), let it expire -> attempt becomes 2
    ps.pull("s")
    time.sleep(0.25)

    # Pull again (attempt 2 internally via reclaim), expire again -> attempt becomes 3 > 2
    ps.pull("s")
    time.sleep(0.25)

    # Now reclaim triggers dead-letter forwarding
    ps.pull("s")  # triggers reclaim which checks dead-letter

    # DL topic should have the message
    dl_msgs = ps.pull("dl-sub")
    assert len(dl_msgs) >= 1


def test_stats_includes_dead_letter_policy():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_topic("dl")
    dl_policy = DeadLetterPolicy(dead_letter_topic="dl", max_delivery_attempts=3)
    ps.create_subscription("s", "t", dead_letter_policy=dl_policy)
    stats = ps.stats("s")
    assert "deadLetterPolicy" in stats
    assert stats["deadLetterPolicy"]["deadLetterTopic"] == "dl"
    assert stats["deadLetterPolicy"]["maxDeliveryAttempts"] == 3


# ----- modify_ack_deadline -----

def test_modify_ack_deadline_extends():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t", ack_deadline=0.2)
    ps.publish("t", b"data")
    msgs = ps.pull("s")
    ack_id = msgs[0]["ackId"]

    # Extend deadline to 10s
    ok = ps.modify_ack_deadline("s", ack_id, 10.0)
    assert ok is True

    # After 0.3s the original deadline would have expired, but we extended it
    time.sleep(0.3)
    again = ps.pull("s")
    assert again == [], "message should not be redelivered yet"

    # Ack it
    ps.ack("s", [ack_id])
    assert ps.stats("s")["outstanding"] == 0


def test_modify_ack_deadline_missing_ack_id():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ok = ps.modify_ack_deadline("s", "nonexistent", 10.0)
    assert ok is False


def test_modify_ack_deadline_shrink_causes_redeliver():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t", ack_deadline=30.0)
    ps.publish("t", b"y")
    msgs = ps.pull("s")
    ack_id = msgs[0]["ackId"]

    # Shrink deadline to nearly 0
    ps.modify_ack_deadline("s", ack_id, 0.1)
    time.sleep(0.25)

    redelivered = ps.pull("s")
    assert len(redelivered) == 1


# ----- push delivery -----

def test_push_handler_receives_messages():
    ps = PubSub()
    received = []

    ps.create_topic("t")
    ps.create_subscription("push-sub", "t",
                           push_handler=lambda msg: received.append(msg))

    ps.publish("t", b"pushed1")
    ps.publish("t", b"pushed2")

    # Push loop runs in a daemon thread; give it time to deliver
    deadline = time.time() + 3.0
    while len(received) < 2 and time.time() < deadline:
        time.sleep(0.05)

    assert len(received) == 2
    import base64
    bodies = {base64.b64decode(m["data"]) for m in received}
    assert bodies == {b"pushed1", b"pushed2"}


def test_push_handler_nacks_on_exception():
    ps = PubSub()
    call_count = [0]

    def flaky(msg):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("first attempt fails")
        # second attempt succeeds silently

    ps.create_topic("t")
    ps.create_subscription("push-sub2", "t", ack_deadline=0.5,
                           push_handler=flaky)
    ps.publish("t", b"retry-me")

    deadline = time.time() + 3.0
    while call_count[0] < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert call_count[0] >= 2


# ----- update_subscription -----

def test_update_subscription_ack_deadline():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t", ack_deadline=10.0)
    ps.update_subscription("s", ack_deadline=20.0)
    assert ps.stats("s")["ackDeadline"] == 20.0


def test_update_subscription_dead_letter():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_topic("dl")
    ps.create_subscription("s", "t")
    assert "deadLetterPolicy" not in ps.stats("s")

    dl = DeadLetterPolicy("dl", max_delivery_attempts=4)
    ps.update_subscription("s", dead_letter_policy=dl)
    stats = ps.stats("s")
    assert "deadLetterPolicy" in stats
    assert stats["deadLetterPolicy"]["maxDeliveryAttempts"] == 4


def test_ordering_key_stored_in_message():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ps.publish("t", b"x", ordering_key="mykey")
    msgs = ps.pull("s")
    assert msgs[0]["message"]["orderingKey"] == "mykey"
