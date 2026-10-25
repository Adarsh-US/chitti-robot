#!/usr/bin/env python3
"""Stage 3 MQTT brain for Jarvis Home.

The brain only communicates with the other project components through MQTT.
It keeps a small in-memory view of the house, asks Gemini for decisions when
events occur, and applies a local high-temperature safety rule before any LLM
response is considered.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import select
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt


SENSORS = ("temp", "motion", "door", "light", "fan")
ALLOWED_COMMANDS = {
    "move_to",
    "stop",
    "fan_on",
    "fan_off",
    "light_on",
    "light_off",
}
DEFAULT_MODEL = "gemini-3.5-flash-lite"
LLM_COOLDOWN_SECONDS = 10.0
GEMINI_MIN_DEADLINE_SECONDS = 10.0
HOT_TEMPERATURE = 50.0
DEFAULT_HOUSE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "house.json"

LOGGER = logging.getLogger("jarvis.brain")


def _house_config_path() -> Path:
    configured_path = os.environ.get("JARVIS_HOUSE_CONFIG")
    if configured_path:
        return Path(configured_path).expanduser()
    return DEFAULT_HOUSE_CONFIG_PATH


def _load_room_definitions(path: Path) -> tuple[dict[str, dict[str, object]], dict[int, str]]:
    """Load the configured room IDs and presentation metadata."""

    try:
        with path.open(encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unable to load house config {path}: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("rooms"), list):
        raise ValueError(f"house config {path} must contain a rooms list")

    floors: dict[int, str] = {}
    raw_floors = config.get("floors", {})
    if isinstance(raw_floors, dict):
        for raw_floor, raw_data in raw_floors.items():
            try:
                floor = int(raw_floor)
            except (TypeError, ValueError):
                continue
            display_name = raw_data.get("display_name") if isinstance(raw_data, dict) else None
            floors[floor] = str(display_name) if display_name is not None else f"Floor {floor}"

    definitions: dict[str, dict[str, object]] = {}
    for raw_room in config["rooms"]:
        if not isinstance(raw_room, dict):
            raise ValueError(f"house config {path} contains a non-object room")
        room_id = raw_room.get("id")
        if not isinstance(room_id, str) or not room_id:
            raise ValueError(f"house config {path} contains a room without an ID")
        if room_id in definitions:
            raise ValueError(f"house config {path} contains duplicate room ID {room_id!r}")
        raw_floor = raw_room.get("floor")
        if isinstance(raw_floor, bool):
            raise ValueError(f"house config {path} has an invalid floor for {room_id!r}")
        try:
            floor = int(raw_floor)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"house config {path} has an invalid floor for {room_id!r}") from exc
        display_name = raw_room.get("display_name", room_id)
        definitions[room_id] = {
            "display_name": str(display_name),
            "floor": floor,
        }
        floors.setdefault(floor, f"Floor {floor}")
    if not definitions:
        raise ValueError(f"house config {path} must define at least one room")
    return definitions, floors


ROOM_DEFINITIONS, FLOOR_NAMES = _load_room_definitions(_house_config_path())
ROOMS = tuple(ROOM_DEFINITIONS)
COMMAND_ROOMS = set(ROOMS)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _compact_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _strip_code_fence(text: str) -> str:
    """Remove one optional Markdown JSON fence from an LLM response."""

    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else stripped


class JarvisBrain:
    """MQTT state store, LLM decision maker, and safety controller."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        timeout: float = 5.0,
        model: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.model = model or os.environ.get("JARVIS_MODEL", DEFAULT_MODEL)
        self.rooms: dict[str, dict[str, object | None]] = {
            room: {sensor: None for sensor in SENSORS} for room in ROOMS
        }
        self.robot_state: dict[str, object] = {}
        self._safety_latched = {room: False for room in ROOMS}
        self._state_lock = threading.RLock()
        self._llm_lock = threading.Lock()
        self._last_llm_request = float("-inf")
        self._gemini_client: Any | None = None
        self._connected = threading.Event()
        self._failure: str | None = None
        self._subscriptions = 0
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"jarvis-stage3-{uuid.uuid4().hex[:12]}",
        )
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    @property
    def connected(self) -> threading.Event:
        """Expose the connection event for integration tests and callers."""

        return self._connected

    def state_summary(self) -> str:
        """Return the compact JSON state sent to the model."""

        with self._state_lock:
            rooms_by_floor: dict[str, dict[str, object]] = {}
            for room, sensor_state in self.rooms.items():
                definition = ROOM_DEFINITIONS[room]
                floor = int(definition["floor"])
                floor_key = str(floor)
                floor_state = rooms_by_floor.setdefault(
                    floor_key,
                    {
                        "display_name": FLOOR_NAMES.get(floor, f"Floor {floor}"),
                        "floor": floor,
                        "rooms": {},
                    },
                )
                room_state = sensor_state.copy()
                room_state.update(
                    {
                        "display_name": definition["display_name"],
                        "floor": floor,
                    }
                )
                floor_state["rooms"][room] = room_state  # type: ignore[index]

            robot = self.robot_state.copy()
            if "floor" not in robot:
                robot_room = robot.get("room")
                if isinstance(robot_room, str) and robot_room in ROOM_DEFINITIONS:
                    robot["floor"] = ROOM_DEFINITIONS[robot_room]["floor"]
            state = {"rooms": rooms_by_floor, "robot": robot}
            return _compact_json(state)

    def state_snapshot(self) -> dict[str, object]:
        """Return a detached state snapshot useful to local callers/tests."""

        with self._state_lock:
            return {
                "rooms": {room: values.copy() for room, values in self.rooms.items()},
                "robot": self.robot_state.copy(),
            }

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: dict[str, object],
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            self._failure = f"broker rejected connection: {reason_code}"
            return
        self._subscriptions = 0
        for topic in ("house/#", "robot/state", "user/speech"):
            result, _ = client.subscribe(topic, qos=1)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self._failure = f"subscription failed for {topic}: {mqtt.error_string(result)}"
                return

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: object,
        mid: int,
        reason_codes: list[mqtt.ReasonCode],
        properties: mqtt.Properties | None,
    ) -> None:
        if any(reason_code.is_failure for reason_code in reason_codes):
            self._failure = "broker rejected a brain subscription"
            return
        self._subscriptions += 1
        if self._subscriptions >= 3:
            self._connected.set()

    def connect(self) -> None:
        """Connect to MQTT and wait until all brain subscriptions are active."""

        try:
            self.client.connect(self.host, self.port, keepalive=30)
        except OSError as exc:
            raise RuntimeError(f"broker unavailable at {self.host}:{self.port}: {exc}") from exc
        self.client.loop_start()
        if not self._connected.wait(timeout=self.timeout):
            detail = f": {self._failure}" if self._failure else ""
            self.close()
            raise RuntimeError(f"timed out subscribing to house/#, robot/state, and user/speech{detail}")
        LOGGER.info("MQTT connected to %s:%s; subscribed house/#, robot/state, user/speech", self.host, self.port)

    def close(self) -> None:
        """Stop the MQTT network loop."""

        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def _on_message(self, client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage) -> None:
        try:
            body = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOGGER.warning("ignored invalid JSON on %s", message.topic)
            return
        if message.topic == "robot/state":
            self._handle_robot_state(body)
            return
        if message.topic == "user/speech":
            self._handle_speech(body)
            return
        self._handle_sensor(message.topic, body)

    def handle_message(self, topic: str, payload: object) -> None:
        """Process a decoded MQTT message, primarily for deterministic tests."""

        if topic == "robot/state":
            self._handle_robot_state(payload)
        elif topic == "user/speech":
            self._handle_speech(payload)
        else:
            self._handle_sensor(topic, payload)

    def _handle_robot_state(self, body: object) -> None:
        if not isinstance(body, dict):
            LOGGER.warning("ignored non-object robot/state payload")
            return
        if not _is_number(body.get("ts")):
            LOGGER.warning("ignored robot/state payload without numeric ts")
            return
        with self._state_lock:
            self.robot_state = dict(body)

    def _handle_speech(self, body: object) -> None:
        if not isinstance(body, dict) or not _is_number(body.get("ts")) or not isinstance(body.get("text"), str):
            LOGGER.warning("ignored invalid user/speech payload")
            return
        text = body["text"].strip()
        if not text:
            return
        self._request_decision(f"User speech: {text}")

    def _handle_sensor(self, topic: str, body: object) -> None:
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "house" or parts[1] not in COMMAND_ROOMS or parts[2] not in SENSORS:
            return
        room, sensor = parts[1], parts[2]
        if not isinstance(body, dict) or not _is_number(body.get("ts")):
            LOGGER.warning("ignored invalid sensor payload on %s", topic)
            return
        if sensor not in body:
            LOGGER.warning("ignored sensor payload without %s on %s", sensor, topic)
            return
        value = body[sensor]
        if sensor == "temp":
            valid = _is_number(value)
        else:
            valid = isinstance(value, bool)
        if not valid:
            LOGGER.warning("ignored invalid %s value on %s", sensor, topic)
            return

        with self._state_lock:
            previous = self.rooms[room][sensor]
            self.rooms[room][sensor] = value
            safety = self._safety_action_locked(room)
        if safety is not None:
            command, reason = safety
            self._publish_command(command)
            self._publish_decision("safety", reason, [command])
        important_change = previous is not None and (
            abs(float(value) - float(previous)) >= 1.0
            if sensor == "temp"
            else previous != value
        )
        if important_change:
            self._request_decision(f"Sensor change on {room}/{sensor}: {sensor}={value}")

    def _safety_action_locked(self, room: str) -> tuple[dict[str, object], str] | None:
        """Update the per-room hot incident latch; caller holds state lock."""

        temperature = self.rooms[room]["temp"]
        fan = self.rooms[room]["fan"]
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or temperature <= HOT_TEMPERATURE:
            self._safety_latched[room] = False
            return None
        if fan is True:
            self._safety_latched[room] = False
            return None
        if self._safety_latched[room]:
            return None
        self._safety_latched[room] = True
        command = {"command": "fan_on", "room": room}
        return command, f"Safety override: {room} temperature exceeds {HOT_TEMPERATURE:g}C; turning on the fan"

    def _request_decision(self, event: str) -> None:
        """Ask the model if the global ten-second gate permits a request."""

        with self._llm_lock:
            now = time.monotonic()
            if now - self._last_llm_request < LLM_COOLDOWN_SECONDS:
                return
            self._last_llm_request = now

        response = self._call_llm(event)
        if response is None:
            return
        parsed = self._parse_decision(response)
        if parsed is None:
            return
        decision, reason, commands = parsed
        for command in commands:
            self._publish_command(command)
        self._publish_decision(decision, reason, commands)

    def _call_llm(self, event: str) -> str | None:
        # This override is intentionally test-only and is inactive unless the
        # environment variable is explicitly present.
        if "JARVIS_TEST_LLM_RESPONSE" in os.environ:
            return os.environ["JARVIS_TEST_LLM_RESPONSE"]

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            LOGGER.warning("LLM unavailable")
            return None
        try:
            from google import genai
            from google.genai import types

            if self._gemini_client is None:
                self._gemini_client = genai.Client(
                    api_key=api_key,
                    http_options=types.HttpOptions(
                        timeout=int(max(self.timeout, GEMINI_MIN_DEADLINE_SECONDS) * 1000)
                    ),
                )
            response = self._gemini_client.models.generate_content(
                model=self.model,
                contents=(
                    f"{event}\nCurrent state: {self.state_summary()}\n\n"
                    "Return one JSON object with exactly this shape: "
                    '{"decision":"...","reason":"...","commands":[{"command":"...","room":"..."}]}. '
                    "The commands array may be empty. Each command object must use the exact key "
                    "'command' and may use the exact key 'room'; never use device, action, target, "
                    "or any other command keys. The command value must be exactly one of "
                    "move_to, stop, fan_on, fan_off, light_on, light_off. The room value, when "
                    f"present, must be exactly one of {', '.join(ROOMS)}. "
                    "move_to requires room; fan/light commands should include room when targeting "
                    f"a specific room. Configured rooms grouped by floor: {self._room_prompt()}. "
                    "The current robot floor is included in the state under robot.floor."
                ),
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "You are Jarvis Home. Make safe, concise house-control decisions. "
                        "Return JSON only."
                    ),
                    response_mime_type="application/json",
                    max_output_tokens=512,
                ),
            )
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                LOGGER.warning("invalid LLM response: empty text")
                return None
            return text
        except Exception as exc:  # API, import, and transport failures should not kill MQTT.
            LOGGER.warning("LLM unavailable: %s", type(exc).__name__)
            return None

    @staticmethod
    def _room_prompt() -> str:
        """Return configured room IDs grouped with their floor metadata."""

        grouped: dict[int, list[str]] = {}
        for room in ROOMS:
            floor = int(ROOM_DEFINITIONS[room]["floor"])
            grouped.setdefault(floor, []).append(room)
        return "; ".join(
            f"{FLOOR_NAMES.get(floor, f'Floor {floor}')} (floor {floor}): "
            + ", ".join(
                f"{room} ({ROOM_DEFINITIONS[room]['display_name']})" for room in grouped[floor]
            )
            for floor in sorted(grouped)
        )

    def _parse_decision(self, response: str) -> tuple[str, str, list[dict[str, object]]] | None:
        try:
            decoded = json.loads(_strip_code_fence(response))
        except (TypeError, json.JSONDecodeError):
            LOGGER.warning("invalid LLM response: expected JSON object")
            return None
        if not isinstance(decoded, dict):
            LOGGER.warning("invalid LLM response: expected JSON object")
            return None
        decision = decoded.get("decision")
        reason = decoded.get("reason")
        raw_commands = decoded.get("commands")
        if not isinstance(decision, str) or not decision.strip() or not isinstance(reason, str) or not isinstance(raw_commands, list):
            LOGGER.warning("invalid LLM response: missing decision, reason, or commands")
            return None

        commands: list[dict[str, object]] = []
        for raw_command in raw_commands:
            command = self._validate_command(raw_command)
            if command is None:
                LOGGER.warning("dropped invalid LLM command")
            else:
                commands.append(command)
        return decision, reason, commands

    def _validate_command(self, raw_command: object) -> dict[str, object] | None:
        if not isinstance(raw_command, dict):
            return None
        name = raw_command.get("command")
        if not isinstance(name, str) or name not in ALLOWED_COMMANDS:
            return None
        has_room = "room" in raw_command
        room = raw_command.get("room")
        if has_room and (not isinstance(room, str) or room not in COMMAND_ROOMS):
            return None
        if name == "move_to" and not has_room:
            return None
        if name in {"fan_on", "fan_off", "light_on", "light_off"} and not has_room:
            with self._state_lock:
                room = self.robot_state.get("room")
            if not isinstance(room, str) or room not in COMMAND_ROOMS:
                return None
            has_room = True
        result: dict[str, object] = {"command": name}
        if has_room:
            result["room"] = room
        return result

    def _publish_command(self, command: dict[str, object]) -> None:
        body = {"ts": float(time.time()), **command}
        self._publish("robot/command", body)

    def _publish_decision(self, decision: str, reason: str, commands: list[dict[str, object]]) -> None:
        self._publish(
            "jarvis/decision",
            {"ts": float(time.time()), "decision": decision, "reason": reason, "commands": commands},
        )

    def _publish(self, topic: str, body: dict[str, object]) -> None:
        info = self.client.publish(topic, _compact_json(body), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.warning("MQTT publish failed on %s: %s", topic, mqtt.error_string(info.rc))

    def publish_speech(self, text: str) -> None:
        """Publish one terminal line to the speech topic."""

        self._publish("user/speech", {"ts": float(time.time()), "text": text})

    def run(self, duration: float | None = None, no_input: bool = False) -> None:
        """Run the terminal input loop, optionally ending after ``duration`` seconds."""

        started = time.monotonic()
        while duration is None or time.monotonic() - started < duration:
            remaining = None if duration is None else max(0.0, duration - (time.monotonic() - started))
            if no_input:
                time.sleep(min(0.1, remaining) if remaining is not None else 0.1)
                continue
            wait = 0.25 if remaining is None else min(0.25, remaining)
            try:
                readable, _, _ = select.select([sys.stdin], [], [], wait)
            except (OSError, ValueError):
                time.sleep(wait)
                continue
            if not readable:
                continue
            line = sys.stdin.readline()
            if line == "":
                if duration is None:
                    time.sleep(0.25)
                    continue
                break
            text = line.rstrip("\r\n")
            if text:
                self.publish_speech(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--timeout", type=float, default=5.0, help="MQTT/LLM timeout in seconds")
    parser.add_argument(
        "--model",
        default=None,
        help="Gemini model name (overrides JARVIS_MODEL; default: gemini-3.5-flash-lite)",
    )
    parser.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    parser.add_argument("--no-input", action="store_true", help="disable terminal input")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parser().parse_args(argv)
    if args.port <= 0 or args.port > 65535 or args.timeout <= 0 or (args.duration is not None and args.duration < 0):
        print("jarvis brain error: port must be 1-65535, timeout positive, and duration non-negative", file=sys.stderr)
        return 2
    brain = JarvisBrain(args.host, args.port, args.timeout, args.model)
    try:
        brain.connect()
        brain.run(args.duration, args.no_input)
    except (RuntimeError, OSError) as exc:
        print(f"jarvis brain error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        LOGGER.info("Stage 3 brain stopped")
    finally:
        brain.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
