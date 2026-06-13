import time

import pytest

from opengcp.pubsub import (PubSub, TopicNotFound, SubscriptionNotFound,
                            PubSubError)


def test_create_topic_and_subscription():
    ps = PubSub()
    ps.create_topic("orders")
    ps.create_subscription("worker", "orders")
    assert ps.list_topics() == ["orders"]
    assert ps.list_subscriptions("orders") == ["worker"]


def test_subscription_requires_topic():
    ps = PubSub()
    with pytest.raises(TopicNotFound):
        ps.create_subscription("s", "missing")


def test_publish_pull_ack_roundtrip():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    mid = ps.publish("t", b"payload", attributes={"k": "v"})
    received = ps.pull("s", max_messages=10)
    assert len(received) == 1
    assert received[0]["message"]["messageId"] == mid
    assert received[0]["message"]["attributes"] == {"k": "v"}
    # now outstanding, not available
    assert ps.stats("s")["outstanding"] == 1
    assert ps.pull("s") == []
    acked = ps.ack("s", [received[0]["ackId"]])
    assert acked == 1
    assert ps.stats("s")["outstanding"] == 0


def test_fanout_to_multiple_subscriptions():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("a", "t")
    ps.create_subscription("b", "t")
    ps.publish("t", "hi")
    assert len(ps.pull("a")) == 1
    assert len(ps.pull("b")) == 1


def test_nack_redelivers():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ps.publish("t", "x")
    msgs = ps.pull("s")
    ack_id = msgs[0]["ackId"]
    ps.nack("s", [ack_id])
    again = ps.pull("s")
    assert len(again) == 1
    assert again[0]["message"]["deliveryAttempt"] == 2


def test_ack_deadline_redelivery():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t", ack_deadline=0.2)
    ps.publish("t", "x")
    ps.pull("s")  # becomes outstanding
    assert ps.pull("s") == []
    time.sleep(0.3)
    # expired -> reclaimed on next pull
    again = ps.pull("s")
    assert len(again) == 1


def test_publish_to_missing_topic():
    ps = PubSub()
    with pytest.raises(TopicNotFound):
        ps.publish("nope", "x")


def test_delete_topic_removes_subscriptions():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ps.delete_topic("t")
    assert ps.list_topics() == []
    with pytest.raises(SubscriptionNotFound):
        ps.pull("s")


def test_delete_subscription():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ps.delete_subscription("s")
    assert ps.list_subscriptions() == []


def test_string_data_encoded_as_utf8():
    ps = PubSub()
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ps.publish("t", "café")
    import base64
    msg = ps.pull("s")[0]["message"]
    assert base64.b64decode(msg["data"]).decode("utf-8") == "café"


def test_publish_hook_fires():
    ps = PubSub()
    seen = []
    ps.add_publish_hook(lambda topic, msg: seen.append((topic, msg.data)))
    ps.create_topic("t")
    ps.create_subscription("s", "t")
    ps.publish("t", b"hooked")
    assert seen == [("t", b"hooked")]
