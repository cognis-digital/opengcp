"""Pub/Sub-style message broker (topics + subscriptions + pull/ack).

Implements a compatible SUBSET of the Cloud Pub/Sub model:
publishers send messages to a topic; each subscription attached to that topic
receives an independent copy. Subscribers ``pull`` messages (which become
"outstanding" with an ack deadline) and ``ack`` them to remove them, or ``nack``
to make them immediately available again. Expired outstanding messages are
automatically redelivered.

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional


class PubSubError(Exception):
    pass


class TopicNotFound(PubSubError):
    pass


class SubscriptionNotFound(PubSubError):
    pass


@dataclass
class Message:
    message_id: str
    data: bytes
    attributes: Dict[str, str]
    publish_time: float
    delivery_attempt: int = 1

    def to_dict(self) -> dict:
        return {
            "messageId": self.message_id,
            "data": base64.b64encode(self.data).decode("ascii"),
            "attributes": dict(self.attributes),
            "publishTime": self.publish_time,
            "deliveryAttempt": self.delivery_attempt,
        }


@dataclass
class _Outstanding:
    message: Message
    ack_id: str
    deadline: float


@dataclass
class _Subscription:
    name: str
    topic: str
    ack_deadline: float = 10.0
    available: Deque[Message] = field(default_factory=deque)
    outstanding: Dict[str, _Outstanding] = field(default_factory=dict)


class PubSub:
    """Thread-safe in-process Pub/Sub broker."""

    def __init__(self):
        self._lock = threading.RLock()
        self._topics: Dict[str, List[str]] = {}  # topic -> [subscription names]
        self._subs: Dict[str, _Subscription] = {}
        # publish hooks let the FunctionRunner observe published messages
        self._publish_hooks: List[Callable[[str, Message], None]] = []

    # ----- topic operations -----
    def create_topic(self, topic: str) -> None:
        with self._lock:
            if topic in self._topics:
                raise PubSubError(f"topic exists: {topic}")
            self._topics[topic] = []

    def list_topics(self) -> List[str]:
        with self._lock:
            return sorted(self._topics)

    def delete_topic(self, topic: str) -> None:
        with self._lock:
            if topic not in self._topics:
                raise TopicNotFound(topic)
            for sub in self._topics[topic]:
                self._subs.pop(sub, None)
            del self._topics[topic]

    # ----- subscription operations -----
    def create_subscription(self, name: str, topic: str,
                            ack_deadline: float = 10.0) -> None:
        with self._lock:
            if topic not in self._topics:
                raise TopicNotFound(topic)
            if name in self._subs:
                raise PubSubError(f"subscription exists: {name}")
            self._subs[name] = _Subscription(name=name, topic=topic,
                                             ack_deadline=ack_deadline)
            self._topics[topic].append(name)

    def list_subscriptions(self, topic: Optional[str] = None) -> List[str]:
        with self._lock:
            if topic is None:
                return sorted(self._subs)
            if topic not in self._topics:
                raise TopicNotFound(topic)
            return sorted(self._topics[topic])

    def delete_subscription(self, name: str) -> None:
        with self._lock:
            if name not in self._subs:
                raise SubscriptionNotFound(name)
            sub = self._subs.pop(name)
            if sub.topic in self._topics:
                self._topics[sub.topic] = [s for s in self._topics[sub.topic]
                                           if s != name]

    # ----- publish / pull / ack -----
    def add_publish_hook(self, hook: Callable[[str, "Message"], None]) -> None:
        with self._lock:
            self._publish_hooks.append(hook)

    def publish(self, topic: str, data, attributes: Optional[Dict[str, str]] = None) -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        with self._lock:
            if topic not in self._topics:
                raise TopicNotFound(topic)
            msg = Message(
                message_id=uuid.uuid4().hex,
                data=data,
                attributes=dict(attributes or {}),
                publish_time=time.time(),
            )
            for sub_name in self._topics[topic]:
                # each subscription gets an independent copy
                copy = Message(msg.message_id, msg.data, dict(msg.attributes),
                               msg.publish_time)
                self._subs[sub_name].available.append(copy)
            hooks = list(self._publish_hooks)
        # fire hooks outside the lock to avoid re-entrancy deadlocks
        for hook in hooks:
            try:
                hook(topic, msg)
            except Exception:
                pass
        return msg.message_id

    def _reclaim_expired(self, sub: _Subscription) -> None:
        now = time.time()
        expired = [aid for aid, o in sub.outstanding.items() if o.deadline <= now]
        for aid in expired:
            o = sub.outstanding.pop(aid)
            o.message.delivery_attempt += 1
            sub.available.appendleft(o.message)

    def pull(self, subscription: str, max_messages: int = 10) -> List[dict]:
        """Return up to ``max_messages`` messages, each as
        ``{"ackId": ..., "message": {...}}``.
        """
        with self._lock:
            if subscription not in self._subs:
                raise SubscriptionNotFound(subscription)
            sub = self._subs[subscription]
            self._reclaim_expired(sub)
            out = []
            while sub.available and len(out) < max_messages:
                msg = sub.available.popleft()
                ack_id = uuid.uuid4().hex
                sub.outstanding[ack_id] = _Outstanding(
                    message=msg, ack_id=ack_id,
                    deadline=time.time() + sub.ack_deadline)
                out.append({"ackId": ack_id, "message": msg.to_dict()})
            return out

    def ack(self, subscription: str, ack_ids: List[str]) -> int:
        with self._lock:
            if subscription not in self._subs:
                raise SubscriptionNotFound(subscription)
            sub = self._subs[subscription]
            n = 0
            for aid in ack_ids:
                if sub.outstanding.pop(aid, None) is not None:
                    n += 1
            return n

    def nack(self, subscription: str, ack_ids: List[str]) -> int:
        with self._lock:
            if subscription not in self._subs:
                raise SubscriptionNotFound(subscription)
            sub = self._subs[subscription]
            n = 0
            for aid in ack_ids:
                o = sub.outstanding.pop(aid, None)
                if o is not None:
                    o.message.delivery_attempt += 1
                    sub.available.appendleft(o.message)
                    n += 1
            return n

    def stats(self, subscription: str) -> dict:
        with self._lock:
            if subscription not in self._subs:
                raise SubscriptionNotFound(subscription)
            sub = self._subs[subscription]
            self._reclaim_expired(sub)
            return {
                "subscription": subscription,
                "topic": sub.topic,
                "available": len(sub.available),
                "outstanding": len(sub.outstanding),
            }
