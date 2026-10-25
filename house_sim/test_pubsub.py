#!/usr/bin/env python3
"""Prove the local MQTT broker can publish and receive a test temperature."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt


TOPIC = "house/test/temp"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--timeout", type=float, default=5.0, help="test timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout <= 0:
        print("PUBSUB TEST FAILED: --timeout must be positive", file=sys.stderr)
        return 2

    ready = threading.Event()
    received = threading.Event()
    errors: list[str] = []
    payload = {"ts": time.time(), "room": "test", "temp": 21.5}
    received_payload: list[dict[str, object]] = []

    subscriber = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"jarvis-test-sub-{uuid.uuid4().hex[:10]}",
    )

    def on_subscriber_connect(
        client: mqtt.Client,
        userdata: object,
        flags: dict[str, object],
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            errors.append(f"subscriber connection rejected: {reason_code}")
            return
        result, _ = client.subscribe(TOPIC, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            errors.append(f"subscriber subscribe failed: {mqtt.error_string(result)}")

    def on_subscribe(
        client: mqtt.Client,
        userdata: object,
        mid: int,
        reason_codes: list[mqtt.ReasonCode],
        properties: mqtt.Properties | None,
    ) -> None:
        if any(reason_code.is_failure for reason_code in reason_codes):
            errors.append("broker rejected the test subscription")
            return
        ready.set()

    def on_message(client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage) -> None:
        if message.topic != TOPIC:
            return
        try:
            body = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append("received a non-JSON payload")
            return
        if isinstance(body, dict):
            received_payload.append(body)
            received.set()

    subscriber.on_connect = on_subscriber_connect
    subscriber.on_subscribe = on_subscribe
    subscriber.on_message = on_message

    publisher = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"jarvis-test-pub-{uuid.uuid4().hex[:10]}",
    )
    publisher_connected = threading.Event()

    def on_publisher_connect(
        client: mqtt.Client,
        userdata: object,
        flags: dict[str, object],
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            errors.append(f"publisher connection rejected: {reason_code}")
            return
        publisher_connected.set()

    publisher.on_connect = on_publisher_connect

    try:
        try:
            subscriber.connect(args.host, args.port, keepalive=30)
            publisher.connect(args.host, args.port, keepalive=30)
        except OSError as exc:
            print(f"PUBSUB TEST FAILED: broker unavailable at {args.host}:{args.port}: {exc}", file=sys.stderr)
            return 2

        subscriber.loop_start()
        publisher.loop_start()
        deadline = time.monotonic() + args.timeout
        if not ready.wait(timeout=max(0.01, deadline - time.monotonic())):
            detail = f": {errors[0]}" if errors else ""
            print(f"PUBSUB TEST FAILED: subscriber was not ready{detail}", file=sys.stderr)
            return 1
        if not publisher_connected.wait(timeout=max(0.01, deadline - time.monotonic())):
            detail = f": {errors[0]}" if errors else ""
            print(f"PUBSUB TEST FAILED: publisher was not ready{detail}", file=sys.stderr)
            return 1

        encoded = json.dumps(payload, separators=(",", ":"))
        info = publisher.publish(TOPIC, encoded, qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"PUBSUB TEST FAILED: publish failed: {mqtt.error_string(info.rc)}", file=sys.stderr)
            return 1
        info.wait_for_publish(timeout=max(0.01, deadline - time.monotonic()))
        if not info.is_published():
            print("PUBSUB TEST FAILED: publish acknowledgement timed out", file=sys.stderr)
            return 1
        print(f"published {TOPIC}: {encoded}")

        if not received.wait(timeout=max(0.01, deadline - time.monotonic())):
            print("PUBSUB TEST FAILED: timed out waiting for the subscribed message", file=sys.stderr)
            return 1
        actual = received_payload[-1]
        if actual != payload:
            print(f"PUBSUB TEST FAILED: received payload did not match: {actual}", file=sys.stderr)
            return 1
        print(f"received {TOPIC}: {json.dumps(actual, separators=(',', ':'))}")
        print("PUBSUB TEST PASSED")
        return 0
    finally:
        publisher.disconnect()
        subscriber.disconnect()
        publisher.loop_stop()
        subscriber.loop_stop()


if __name__ == "__main__":
    raise SystemExit(main())
