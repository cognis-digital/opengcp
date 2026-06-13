"""Pub/Sub-style message broker (topics + subscriptions + pull/ack).

Implements a compatible SUBSET of the Cloud Pub/Sub model:
publishers send messages to a topic; each subscription attached to that topic
receives an independent copy. Subscribers ``pull`` messages (which become
"outstanding" with an ack deadline) and ``ack`` them to remove them, or ``nack``
to make them immediately available again. Expired outstanding messages are
automatically redelivered.

Extended features in this version:
  * Ordering keys: messages with the same ordering key are delivered in order
    per subscription; a key is "blocked" until its in-flight message is acked.
  * Dead-letter policy: after ``max_delivery_attempts`` failed ack/nack cycles
    the message is forwarded to a dead-letter topic.
  * Configurable ack-deadline per subscription with ``modify_ack_deadline``
    to extend individual messages.
  * Push delivery: subscriptions with a registered push handler callable receive
    messages automatically (fire-and-forget in a daemon thread).

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
    ordering_key: str = ""

    def to_dict(self) -> dict:
        return {
            "messageId": self.message_id,
            "data": base64.b64encode(self.data).decode("ascii"),
            "attributes": dict(self.attributes),
            "publishTime": self.publish_time,
            "deliveryAttempt": self.delivery_attempt,
            "orderingKey": self.ordering_key,
        }


@dataclass
class _Outstanding:
    message: Message
    ack_id: str
    deadline: float


@dataclass
class DeadLetterPolicy:
    dead_letter_topic: str
    max_delivery_attempts: int = 5


@dataclass
class _Subscription:
    name: str
    topic: str
    ack_deadline: float = 10.0
    available: Deque[Message] = field(default_factory=deque)
    outstanding: Dict[str, _Outstanding] = field(default_factory=dict)
    dead_letter_policy: Optional[DeadLetterPolicy] = None
    # push handler: if set, messages are automatically delivered to this callable
    push_handler: Optional[Callable[[dict], None]] = None
    # ordering: key -> ack_id currently in-flight (blocks further delivery for that key)
    _ordering_blocked: Dict[str, str] = field(default_factory=dict)


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
                            ack_deadline: float = 10.0,
                            dead_letter_policy: Optional[DeadLetterPolicy] = None,
                            push_handler: Optional[Callable[[dict], None]] = None) -> None:
        with self._lock:
            if topic not in self._topics:
                raise TopicNotFound(topic)
            if name in self._subs:
                raise PubSubError(f"subscription exists: {name}")
            sub = _Subscription(name=name, topic=topic, ack_deadline=ack_deadline,
                                dead_letter_policy=dead_letter_policy,
                                push_handler=push_handler)
            self._subs[name] = sub
            self._topics[topic].append(name)
        if push_handler is not None:
            self._start_push_loop(name)

    def update_subscription(self, name: str, *,
                            ack_deadline: Optional[float] = None,
                            dead_letter_policy: Optional[DeadLetterPolicy] = None) -> None:
        """Update mutable subscription fields."""
        with self._lock:
            if name not in self._subs:
                raise SubscriptionNotFound(name)
            sub = self._subs[name]
            if ack_deadline is not None:
                sub.ack_deadline = ack_deadline
            if dead_letter_policy is not None:
                sub.dead_letter_policy = dead_letter_policy

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

    def publish(self, topic: str, data, attributes: Optional[Dict[str, str]] = None,
                ordering_key: str = "") -> str:
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
                ordering_key=ordering_key,
            )
            for sub_name in self._topics[topic]:
                copy = Message(msg.message_id, msg.data, dict(msg.attributes),
                               msg.publish_time, ordering_key=msg.ordering_key)
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
            # check dead-letter
            if (sub.dead_letter_policy is not None and
                    o.message.delivery_attempt > sub.dead_letter_policy.max_delivery_attempts):
                self._send_to_dead_letter(sub, o.message)
            else:
                # unblock ordering key
                key = o.message.ordering_key
                if key and sub._ordering_blocked.get(key) == aid:
                    del sub._ordering_blocked[key]
                sub.available.appendleft(o.message)

    def _send_to_dead_letter(self, sub: _Subscription, message: Message) -> None:
        """Forward a message to the dead-letter topic (best-effort)."""
        dl_topic = sub.dead_letter_policy.dead_letter_topic
        if dl_topic in self._topics:
            try:
                dl_msg = Message(
                    message_id=uuid.uuid4().hex,
                    data=message.data,
                    attributes={**message.attributes,
                                "original_message_id": message.message_id,
                                "original_subscription": sub.name},
                    publish_time=time.time(),
                    ordering_key=message.ordering_key,
                )
                for dl_sub_name in self._topics[dl_topic]:
                    copy = Message(dl_msg.message_id, dl_msg.data,
                                   dict(dl_msg.attributes), dl_msg.publish_time)
                    self._subs[dl_sub_name].available.append(copy)
            except Exception:
                pass

    def pull(self, subscription: str, max_messages: int = 10) -> List[dict]:
        """Return up to ``max_messages`` messages, each as
        ``{"ackId": ..., "message": {...}}``.
        Messages with ordering keys are delivered one-at-a-time per key.
        """
        with self._lock:
            if subscription not in self._subs:
                raise SubscriptionNotFound(subscription)
            sub = self._subs[subscription]
            self._reclaim_expired(sub)
            out = []
            available_list = list(sub.available)
            sub.available.clear()
            pending_back = deque()
            for msg in available_list:
                if len(out) >= max_messages:
                    pending_back.append(msg)
                    continue
                # ordering key check: skip if key is currently blocked
                key = msg.ordering_key
                if key and key in sub._ordering_blocked:
                    pending_back.append(msg)
                    continue
                ack_id = uuid.uuid4().hex
                if key:
                    sub._ordering_blocked[key] = ack_id
                sub.outstanding[ack_id] = _Outstanding(
                    message=msg, ack_id=ack_id,
                    deadline=time.time() + sub.ack_deadline)
                out.append({"ackId": ack_id, "message": msg.to_dict()})
            sub.available = pending_back
            return out

    def ack(self, subscription: str, ack_ids: List[str]) -> int:
        with self._lock:
            if subscription not in self._subs:
                raise SubscriptionNotFound(subscription)
            sub = self._subs[subscription]
            n = 0
            for aid in ack_ids:
                o = sub.outstanding.pop(aid, None)
                if o is not None:
                    # unblock ordering key
                    key = o.message.ordering_key
                    if key and sub._ordering_blocked.get(key) == aid:
                        del sub._ordering_blocked[key]
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
                    key = o.message.ordering_key
                    if key and sub._ordering_blocked.get(key) == aid:
                        del sub._ordering_blocked[key]
                    # check dead-letter threshold
                    if (sub.dead_letter_policy is not None and
                            o.message.delivery_attempt >
                            sub.dead_letter_policy.max_delivery_attempts):
                        self._send_to_dead_letter(sub, o.message)
                    else:
                        sub.available.appendleft(o.message)
                    n += 1
            return n

    def modify_ack_deadline(self, subscription: str, ack_id: str,
                            seconds: float) -> bool:
        """Extend (or reduce) the ack deadline for a single outstanding message.

        Returns True if the ack_id was found, False otherwise.
        """
        with self._lock:
            if subscription not in self._subs:
                raise SubscriptionNotFound(subscription)
            sub = self._subs[subscription]
            o = sub.outstanding.get(ack_id)
            if o is None:
                return False
            o.deadline = time.time() + seconds
            return True

    def stats(self, subscription: str) -> dict:
        with self._lock:
            if subscription not in self._subs:
                raise SubscriptionNotFound(subscription)
            sub = self._subs[subscription]
            self._reclaim_expired(sub)
            result = {
                "subscription": subscription,
                "topic": sub.topic,
                "available": len(sub.available),
                "outstanding": len(sub.outstanding),
                "ackDeadline": sub.ack_deadline,
            }
            if sub.dead_letter_policy:
                result["deadLetterPolicy"] = {
                    "deadLetterTopic": sub.dead_letter_policy.dead_letter_topic,
                    "maxDeliveryAttempts": sub.dead_letter_policy.max_delivery_attempts,
                }
            return result

    # ----- push delivery -----
    def _start_push_loop(self, subscription_name: str) -> None:
        """Start a daemon thread that continuously pushes messages to the handler."""
        t = threading.Thread(target=self._push_loop, args=(subscription_name,),
                             daemon=True, name=f"pubsub-push-{subscription_name}")
        t.start()

    def _push_loop(self, subscription_name: str) -> None:
        """Background push loop: pull -> call handler -> ack on success / nack on failure."""
        while True:
            with self._lock:
                sub = self._subs.get(subscription_name)
                if sub is None or sub.push_handler is None:
                    return  # subscription deleted or handler removed
                handler = sub.push_handler
            msgs = self.pull(subscription_name, max_messages=1)
            if not msgs:
                time.sleep(0.05)
                continue
            item = msgs[0]
            ack_id = item["ackId"]
            try:
                handler(item["message"])
                self.ack(subscription_name, [ack_id])
            except Exception:
                try:
                    self.nack(subscription_name, [ack_id])
                except Exception:
                    pass
