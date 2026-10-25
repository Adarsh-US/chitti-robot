#!/usr/bin/env python3
"""Run the config-driven Stage 6A pygame house and robot simulator."""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "house.json"
AMBIENT_TEMPERATURE = 20.5
WINDOW_SIZE = (1400, 760)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load and validate the house definition relative to the project root."""
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict) or not isinstance(config.get("rooms"), list):
        raise ValueError("house config must contain a rooms list")
    room_ids = [room.get("id") for room in config["rooms"] if isinstance(room, dict)]
    expected = {"living-room", "kitchen", "dining-room", "hallway", "study", "landing", "bedroom", "bedroom-2", "bathroom"}
    if set(room_ids) != expected or len(room_ids) != len(expected):
        raise ValueError("house config must define exactly the nine Stage 6A rooms")
    if not isinstance(config.get("floors"), dict) or set(config["floors"]) != {"0", "1"}:
        raise ValueError("house config must define floors 0 and 1")
    return config


def _canonical_room(value: object, aliases: dict[str, str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return aliases.get(normalized) or aliases.get(normalized.replace("_", "-"))


@dataclass
class Room:
    name: str
    display_name: str
    floor: int
    rect: tuple[int, int, int, int]
    doors: tuple[str, ...]
    furniture: tuple[Any, ...]
    floor_offset: float
    temperature: float
    light: bool = False
    door: bool = False
    heat_remaining: float = 0.0
    fan: bool = False

    @property
    def center(self) -> tuple[float, float]:
        x, y, width, height = self.rect
        return x + width / 2, y + height / 2


@dataclass(frozen=True)
class Door:
    name: str
    room_a: str
    room_b: str
    floor: int
    position: tuple[float, float]
    orientation: str


@dataclass(frozen=True)
class Stair:
    name: str
    from_room: str
    to_room: str
    from_position: tuple[float, float]
    to_position: tuple[float, float]
    traversal_time: float
    battery_cost: float


@dataclass(frozen=True)
class Waypoint:
    x: float
    y: float
    floor: int
    room: str
    kind: str
    stair: Stair | None = None


class HouseModel:
    """Mutable model independent of pygame, suitable for model-only checks."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config = load_config(config_path or DEFAULT_CONFIG_PATH)
        self.ambient_temperature = float(self.config.get("ambient_temperature", AMBIENT_TEMPERATURE))
        self.rooms: dict[str, Room] = {}
        self.floor_names: dict[int, str] = {}
        for floor_key, floor_data in self.config["floors"].items():
            self.floor_names[int(floor_key)] = str(floor_data.get("display_name", f"Floor {floor_key}"))
        for raw in self.config["rooms"]:
            floor = int(raw["floor"])
            offset = float(self.config["floors"][str(floor)].get("temperature_offset", 0.0))
            self.rooms[raw["id"]] = Room(
                name=raw["id"],
                display_name=raw["display_name"],
                floor=floor,
                rect=tuple(int(value) for value in raw["rect"]),
                doors=tuple(raw.get("doors", [])),
                furniture=tuple(raw.get("furniture", [])),
                floor_offset=offset,
                temperature=self.ambient_temperature + offset,
            )
        self.room_ids = tuple(self.rooms)
        self.aliases: dict[str, str] = {}
        for room in self.rooms.values():
            self.aliases[room.name] = room.name
            self.aliases[room.display_name.lower()] = room.name
            self.aliases[room.name.replace("-", " ")] = room.name
        self.doors = self._load_doors()
        self.stair = self._load_stair()
        dock = self.config["charging_dock"]
        self.dock_room = str(dock["room"])
        self.dock_position = tuple(float(value) for value in dock["position"])
        self.charge_rate = float(dock.get("charge_rate", 18.0))
        self._graph = self._build_graph()

        self.floor = self.rooms["living-room"].floor
        self.robot_x, self.robot_y = self.rooms["living-room"].center
        self.rooms["living-room"].door = True
        self.target_room: str | None = None
        self.current_task = "idle"
        self.battery = 100.0
        self.person_until: dict[str, float] = {name: 0.0 for name in self.room_ids}
        self.last_event = "Ready — H heat/cook, P add a person"
        self.route: list[Waypoint] = []
        self.route_index = 0
        self._stair_remaining = 0.0
        self._active_stair: Stair | None = None
        self._stair_destination: Waypoint | None = None
        self._route_to_dock = False
        self._lock = threading.RLock()

    def _load_doors(self) -> tuple[Door, ...]:
        result = []
        for raw in self.config["doors"]:
            first, second = raw["rooms"]
            result.append(Door(raw["id"], first, second, self.rooms[first].floor,
                               tuple(float(value) for value in raw["position"]), raw.get("orientation", "vertical")))
        return tuple(result)

    def _load_stair(self) -> Stair:
        raw = self.config["stairs"]
        return Stair(raw["id"], raw["from"], raw["to"],
                     tuple(float(value) for value in raw["from_position"]),
                     tuple(float(value) for value in raw["to_position"]),
                     float(raw["traversal_time"]), float(raw["battery_cost"]))

    def _build_graph(self) -> dict[str, list[tuple[str, float, str, tuple[float, float], Stair | None]]]:
        graph: dict[str, list[tuple[str, float, str, tuple[float, float], Stair | None]]]
        graph = {name: [] for name in self.rooms}
        for door in self.doors:
            a_center, b_center = self.rooms[door.room_a].center, self.rooms[door.room_b].center
            weight = math.dist(a_center, door.position) + math.dist(door.position, b_center)
            graph[door.room_a].append((door.room_b, weight, "door", door.position, None))
            graph[door.room_b].append((door.room_a, weight, "door", door.position, None))
        stair = self.stair
        weight = math.dist(self.rooms[stair.from_room].center, stair.from_position) + stair.traversal_time
        graph[stair.from_room].append((stair.to_room, weight, "stair", stair.from_position, stair))
        graph[stair.to_room].append((stair.from_room, weight, "stair", stair.to_position, stair))
        return graph

    @property
    def robot_room(self) -> str:
        with self._lock:
            if self._active_stair is not None:
                return self._active_stair.from_room if self.floor == self.rooms[self._active_stair.from_room].floor else self._active_stair.to_room
            for name, room in self.rooms.items():
                if room.floor != self.floor:
                    continue
                x, y, width, height = room.rect
                if x <= self.robot_x <= x + width and y <= self.robot_y <= y + height:
                    return name
            return next(name for name, room in self.rooms.items() if room.floor == self.floor)

    def _route_between(self, start: str, target: str, destination: tuple[float, float] | None = None) -> list[Waypoint]:
        if start == target:
            room = self.rooms[target]
            point = destination or room.center
            return [Waypoint(point[0], point[1], room.floor, target, "destination")]
        distances = {start: 0.0}
        previous: dict[str, tuple[str, str, tuple[float, float], Stair | None]] = {}
        queue = [(0.0, start)]
        while queue:
            distance, current = heapq.heappop(queue)
            if distance != distances.get(current):
                continue
            if current == target:
                break
            for neighbor, weight, kind, position, stair in self._graph[current]:
                candidate = distance + weight
                if candidate < distances.get(neighbor, float("inf")):
                    distances[neighbor] = candidate
                    previous[neighbor] = (current, kind, position, stair)
                    heapq.heappush(queue, (candidate, neighbor))
        if target not in distances:
            raise ValueError(f"no route from {start} to {target}")
        edges: list[tuple[str, str, tuple[float, float], Stair | None]] = []
        current = target
        while current != start:
            parent, kind, position, stair = previous[current]
            edges.append((current, kind, position, stair))
            current = parent
        edges.reverse()
        waypoints: list[Waypoint] = []
        current_room = start
        for next_room, kind, position, stair in edges:
            current_floor = self.rooms[current_room].floor
            if kind == "stair" and stair is not None:
                start_position = stair.from_position if current_room == stair.from_room else stair.to_position
                end_position = stair.to_position if current_room == stair.from_room else stair.from_position
                next_floor = self.rooms[next_room].floor
                waypoints.append(Waypoint(start_position[0], start_position[1], current_floor, current_room, "stair_start", stair))
                waypoints.append(Waypoint(end_position[0], end_position[1], next_floor, next_room, "stair_end", stair))
            else:
                waypoints.append(Waypoint(position[0], position[1], current_floor, current_room, "door"))
            center = self.rooms[next_room].center
            waypoints.append(Waypoint(center[0], center[1], self.rooms[next_room].floor, next_room, "room"))
            current_room = next_room
        if destination is not None:
            waypoints.append(Waypoint(destination[0], destination[1], self.rooms[target].floor, target, "dock"))
        elif waypoints and waypoints[-1].kind == "room":
            waypoints[-1] = Waypoint(self.rooms[target].center[0], self.rooms[target].center[1], self.rooms[target].floor, target, "destination")
        return waypoints

    def _begin_route(self, target: str, *, dock: bool = False) -> None:
        waypoints = self._route_between(self.robot_room, target, self.dock_position if dock else None)
        self.route = waypoints
        self.route_index = 0
        self._route_to_dock = dock
        self.target_room = target

    def start_heat(self, room: str | None = None) -> None:
        with self._lock:
            selected = _canonical_room(room, self.aliases) if room is not None else self.robot_room
            if selected is None:
                return
            target = self.rooms[selected]
            target.heat_remaining = max(target.heat_remaining, 10.0)
            target.door = True
            self.last_event = f"Cooking/heat event in {target.name}"

    def add_person(self, room: str | None = None) -> None:
        with self._lock:
            selected = _canonical_room(room, self.aliases) if room is not None else self.robot_room
            if selected is None:
                return
            self.person_until[selected] = max(self.person_until[selected], time.monotonic() + 8.0)
            self.rooms[selected].door = True
            self.last_event = f"Temporary person in {selected} (8s)"

    def process_command(self, payload: object) -> str:
        with self._lock:
            if not isinstance(payload, dict) or not _is_number(payload.get("ts")):
                return "ignored command: missing numeric ts"
            command = payload.get("command")
            if not isinstance(command, str):
                return "ignored command: command must be a string"
            if command == "move_to":
                target = _canonical_room(payload.get("room"), self.aliases)
                if target is None:
                    return "ignored command: move_to needs a known room"
                self._active_stair = None
                self._begin_route(target)
                self.current_task = f"moving to {target}"
                self.rooms[self.robot_room].door = True
                self.rooms[target].door = True
                self.last_event = f"Command: move_to {target}"
                return self.last_event
            if command == "stop":
                self.route = []
                self.route_index = 0
                self.target_room = None
                self._active_stair = None
                self.current_task = "stopped"
                self.last_event = "Command: robot stopped"
                return self.last_event
            if command in {"fan_on", "fan_off"}:
                selected = payload.get("room")
                room_name = self.robot_room if selected is None else _canonical_room(selected, self.aliases)
                if room_name is None:
                    return "ignored command: fan command needs a known room"
                self.rooms[room_name].fan = command == "fan_on"
                self.current_task = command
                self.last_event = f"Command: {command} in {room_name}"
                return self.last_event
            if command in {"light_on", "light_off"}:
                selected = payload.get("room")
                room_name = self.robot_room if selected is None else _canonical_room(selected, self.aliases)
                if room_name is None:
                    return "ignored command: light command needs a known room"
                self.rooms[room_name].light = command == "light_on"
                self.current_task = command
                self.last_event = f"Command: {command} in {room_name}"
                return self.last_event
            return f"ignored command: unsupported {command!r}"

    def _at_dock(self) -> bool:
        return self.floor == self.rooms[self.dock_room].floor and self.robot_room == self.dock_room and math.dist((self.robot_x, self.robot_y), self.dock_position) < 4.0

    def _auto_charge_if_needed(self) -> None:
        if self.battery >= 20.0 or self._at_dock():
            return
        if not self._route_to_dock:
            self._begin_route(self.dock_room, dock=True)
        self.current_task = "charging"

    def _advance_route(self, dt: float) -> None:
        remaining_dt = dt
        while remaining_dt > 0 and self.route_index < len(self.route):
            waypoint = self.route[self.route_index]
            if waypoint.kind == "stair_end" and self._active_stair is None:
                self._active_stair = waypoint.stair
                self._stair_destination = waypoint
                self._stair_remaining = self._active_stair.traversal_time
                self.current_task = "using stairs"
                self.last_event = "Robot using stairs"
                self.route_index += 1
                continue
            if self._active_stair is not None:
                stair = self._active_stair
                interval = min(remaining_dt, self._stair_remaining)
                self._stair_remaining -= interval
                self.battery = max(0.0, self.battery - stair.battery_cost * interval / stair.traversal_time)
                remaining_dt -= interval
                if self._stair_remaining <= 0:
                    destination = self._stair_destination
                    assert destination is not None
                    self.floor, self.robot_x, self.robot_y = destination.floor, destination.x, destination.y
                    self._active_stair = None
                    self._stair_destination = None
                continue
            dx, dy = waypoint.x - self.robot_x, waypoint.y - self.robot_y
            distance = math.hypot(dx, dy)
            travel = 145.0 * remaining_dt
            if distance <= travel:
                self.robot_x, self.robot_y, self.floor = waypoint.x, waypoint.y, waypoint.floor
                self.route_index += 1
                if distance > 0:
                    self.battery = max(0.0, self.battery - 0.018 * distance / 145.0)
                remaining_dt = max(0.0, remaining_dt - distance / 145.0)
            elif distance:
                self.robot_x += dx / distance * travel
                self.robot_y += dy / distance * travel
                self.battery = max(0.0, self.battery - 0.018 * remaining_dt)
                remaining_dt = 0.0
        if self.route_index >= len(self.route) and self._active_stair is None and self.route:
            self.route = []
            self.target_room = None
            if self._route_to_dock:
                self.current_task = "charging"
                self.last_event = "Robot reached charging dock"
            else:
                arrived = self.robot_room
                self.current_task = f"arrived in {arrived}"
                self.last_event = f"Robot arrived in {arrived}"

    def update(self, dt: float, now: float | None = None) -> None:
        if dt <= 0:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            self._auto_charge_if_needed()
            if self.route:
                self._advance_route(dt)
            if self._at_dock():
                self.battery = min(100.0, self.battery + self.charge_rate * dt)
                self.current_task = "charging"
                if self.battery >= 100.0:
                    self._route_to_dock = False
                    self.last_event = "Robot fully charged"
            active_robot_room = self.robot_room
            for name, room in self.rooms.items():
                baseline = self.ambient_temperature + room.floor_offset
                room.temperature += (baseline - room.temperature) * min(1.0, 0.035 * dt)
                if room.heat_remaining > 0:
                    room.temperature += (2.4 if name == "kitchen" else 1.25) * dt
                    room.heat_remaining = max(0.0, room.heat_remaining - dt)
                if room.fan:
                    room.temperature -= min(1.2 * dt, max(0.0, room.temperature - baseline))
                room.door = name == active_robot_room or self.person_until[name] > now or room.heat_remaining > 0

    def room_motion(self, name: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            return name == self.robot_room or self.person_until[name] > now

    def room_payload(self, name: str, now: float | None = None) -> dict[str, object]:
        timestamp = time.time() if now is None else now
        with self._lock:
            room = self.rooms[name]
            return {"ts": float(timestamp), "room": name, "temp": round(room.temperature, 3)}

    def robot_payload(self, now: float | None = None) -> dict[str, object]:
        timestamp = time.time() if now is None else now
        with self._lock:
            return {"ts": float(timestamp), "x": round(self.robot_x, 2), "y": round(self.robot_y, 2),
                    "room": self.robot_room, "floor": self.floor, "battery": round(self.battery, 2),
                    "current_task": self.current_task}


class MqttBridge:
    """MQTT transport for the simulator's fixed topic contract."""

    def __init__(self, model: HouseModel, host: str, port: int, timeout: float) -> None:
        self.model, self.host, self.port, self.timeout = model, host, port, timeout
        self.connected = threading.Event()
        self.failure: str | None = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"jarvis-stage6a-{uuid.uuid4().hex[:12]}")
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    def _on_connect(self, client: mqtt.Client, userdata: object, flags: dict[str, object], reason_code: mqtt.ReasonCode, properties: mqtt.Properties | None) -> None:
        if reason_code.is_failure:
            self.failure = f"broker rejected connection: {reason_code}"
            return
        result, _ = client.subscribe("robot/command", qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self.failure = f"command subscription failed: {mqtt.error_string(result)}"

    def _on_subscribe(self, client: mqtt.Client, userdata: object, mid: int, reason_codes: list[mqtt.ReasonCode], properties: mqtt.Properties | None) -> None:
        if any(reason_code.is_failure for reason_code in reason_codes):
            self.failure = "broker rejected robot/command subscription"
        else:
            self.connected.set()

    def _on_message(self, client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage) -> None:
        if message.topic != "robot/command":
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.model.last_event = f"Ignored invalid command JSON: {exc}"
            return
        result = self.model.process_command(payload)
        if result.startswith("ignored"):
            self.model.last_event = result
            print(result, flush=True)

    def connect(self) -> None:
        try:
            self.client.connect(self.host, self.port, keepalive=30)
        except OSError as exc:
            raise RuntimeError(f"broker unavailable at {self.host}:{self.port}: {exc}") from exc
        self.client.loop_start()
        if not self.connected.wait(timeout=self.timeout):
            detail = f": {self.failure}" if self.failure else ""
            self.close()
            raise RuntimeError(f"timed out subscribing to robot/command{detail}")
        print(f"MQTT connected to {self.host}:{self.port}; subscribed robot/command", flush=True)

    def publish_state(self) -> None:
        timestamp = time.time()
        for name in self.model.room_ids:
            room = self.model.rooms[name]
            messages = (
                (f"house/{name}/temp", self.model.room_payload(name, timestamp)),
                (f"house/{name}/motion", {"ts": float(timestamp), "room": name, "motion": self.model.room_motion(name)}),
                (f"house/{name}/door", {"ts": float(timestamp), "room": name, "door": room.door}),
                (f"house/{name}/light", {"ts": float(timestamp), "room": name, "light": room.light}),
                (f"house/{name}/fan", {"ts": float(timestamp), "room": name, "fan": room.fan}),
            )
            for topic, body in messages:
                self._publish(topic, body)
        self._publish("robot/state", self.model.robot_payload(timestamp))

    def _publish(self, topic: str, body: dict[str, object]) -> None:
        info = self.client.publish(topic, json.dumps(body, separators=(",", ":")), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self.model.last_event = f"MQTT publish failed on {topic}: {mqtt.error_string(info.rc)}"

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


class HouseSimulator:
    """Pygame front end and one-second MQTT publishing loop."""

    def __init__(self, bridge: MqttBridge, headless: bool = False, fps: int = 30) -> None:
        self.model, self.bridge, self.headless, self.fps = bridge.model, bridge, headless, fps
        self.floor_origins = {0: (30, 105), 1: (710, 105)}

    def run(self, duration: float | None = None) -> None:
        if self.headless:
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        try:
            import pygame
        except ImportError as exc:
            raise RuntimeError("pygame is required; install requirements.txt") from exc
        pygame.init()
        screen = pygame.Surface(WINDOW_SIZE) if self.headless else pygame.display.set_mode(WINDOW_SIZE)
        if not self.headless:
            pygame.display.set_caption("Jarvis Home — Stage 6A")
        font, small_font = pygame.font.SysFont("dejavusans", 16), pygame.font.SysFont("dejavusans", 13)
        clock = pygame.time.Clock()
        running, started, last, next_publish = True, time.monotonic(), time.monotonic(), time.monotonic()
        self.bridge.publish_state()
        print("Stage 6A simulator running (H=heat/cook, P=temporary person, Esc=quit)", flush=True)
        try:
            while running and (duration is None or time.monotonic() - started < duration):
                now = time.monotonic()
                dt, last = min(0.25, max(0.0, now - last)), now
                self.model.update(dt, now)
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        running = False
                    elif event.type == pygame.KEYDOWN:
                        if event.key in (pygame.K_ESCAPE, pygame.K_q):
                            running = False
                        elif event.key == pygame.K_h:
                            self.model.start_heat()
                        elif event.key == pygame.K_p:
                            self.model.add_person()
                if now >= next_publish:
                    self.bridge.publish_state()
                    next_publish = now + 1.0
                    print(f"state published; robot={self.model.robot_payload()['room']} floor={self.model.floor} battery={self.model.battery:.1f}%", flush=True)
                self._draw(screen, pygame, font, small_font)
                if not self.headless:
                    pygame.display.flip()
                clock.tick(self.fps)
        finally:
            pygame.quit()

    def _screen_point(self, floor: int, point: tuple[float, float]) -> tuple[int, int]:
        origin = self.floor_origins[floor]
        return round(origin[0] + point[0]), round(origin[1] + point[1])

    def _draw(self, screen: Any, pygame: Any, font: Any, small_font: Any) -> None:
        screen.fill((22, 28, 38))
        with self.model._lock:
            for floor, origin in self.floor_origins.items():
                panel = (origin[0] - 12, origin[1] - 55, 650, 575)
                pygame.draw.rect(screen, (30, 39, 53), panel)
                pygame.draw.rect(screen, (110, 135, 165), panel, 2)
                title = self.model.floor_names.get(floor, f"Floor {floor}")
                screen.blit(font.render(f"{title} (floor {floor})", True, (245, 248, 252)), (origin[0], origin[1] - 38))
                for name, room in self.model.rooms.items():
                    if room.floor != floor:
                        continue
                    x, y, width, height = room.rect
                    rect = (origin[0] + x, origin[1] + y, width, height)
                    motion = self.model.room_motion(name)
                    base = (66, 91, 120) if room.light else (43, 53, 72)
                    if name == self.model.robot_room and floor == self.model.floor:
                        base = (74, 112, 132) if room.light else (48, 74, 88)
                    pygame.draw.rect(screen, base, rect)
                    pygame.draw.rect(screen, (170, 190, 212), rect, 2)
                    screen.blit(font.render(room.display_name, True, (240, 245, 250)), (rect[0] + 10, rect[1] + 10))
                    details = f"{room.temperature:.1f} C  {'motion' if motion else 'quiet'}"
                    details += f"  light={'on' if room.light else 'off'} door={'open' if room.door else 'closed'}"
                    screen.blit(small_font.render(details, True, (220, 230, 238)), (rect[0] + 10, rect[1] + 36))
                    if room.heat_remaining > 0:
                        screen.blit(small_font.render("HEATING / COOKING", True, (255, 190, 80)), (rect[0] + 10, rect[1] + 58))
                    if room.fan:
                        screen.blit(small_font.render("FAN ON", True, (130, 225, 255)), (rect[0] + 10, rect[1] + 76))
                for door in self.model.doors:
                    if door.floor != floor:
                        continue
                    x, y = self._screen_point(floor, door.position)
                    color = (80, 230, 145) if self.model.rooms[door.room_a].door or self.model.rooms[door.room_b].door else (220, 110, 100)
                    if door.orientation == "vertical":
                        pygame.draw.rect(screen, (30, 39, 53), (x - 7, y - 28, 14, 56))
                        pygame.draw.line(screen, color, (x, y - 24), (x + 28, y - 24), 3)
                    else:
                        pygame.draw.rect(screen, (30, 39, 53), (x - 28, y - 7, 56, 14))
                        pygame.draw.line(screen, color, (x - 24, y), (x - 24, y + 28), 3)
                stair_position = self.model.stair.from_position if floor == self.model.rooms[self.model.stair.from_room].floor else self.model.stair.to_position
                sx, sy = self._screen_point(floor, stair_position)
                pygame.draw.rect(screen, (180, 150, 80), (sx - 25, sy - 18, 50, 36), 2)
                for step in range(-15, 20, 8):
                    pygame.draw.line(screen, (180, 150, 80), (sx + step, sy - 15), (sx + step, sy + 15), 1)
                screen.blit(small_font.render("STAIRS", True, (240, 210, 120)), (sx - 23, sy + 21))
                if floor == self.model.rooms[self.model.dock_room].floor:
                    dx, dy = self._screen_point(floor, self.model.dock_position)
                    pygame.draw.circle(screen, (100, 230, 150), (dx, dy), 10, 2)
                    screen.blit(small_font.render("DOCK", True, (150, 245, 180)), (dx - 18, dy - 25))
            rx, ry = self._screen_point(self.model.floor, (self.model.robot_x, self.model.robot_y))
            pygame.draw.circle(screen, (255, 205, 65), (rx, ry), 11)
            pygame.draw.circle(screen, (30, 30, 30), (rx, ry), 11, 2)
            top = f"ROBOT: {self.model.robot_room}  floor {self.model.floor}   battery {self.model.battery:.1f}%   task: {self.model.current_task}"
            screen.blit(font.render(top, True, (250, 250, 250)), (40, 35))
            screen.blit(small_font.render(self.model.last_event, True, (190, 210, 230)), (40, 62))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--timeout", type=float, default=5.0, help="MQTT connection timeout in seconds")
    parser.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    parser.add_argument("--headless", action="store_true", help="run pygame with no display")
    parser.add_argument("--fps", type=int, default=30, help="GUI update rate")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="house JSON path (default: config/house.json)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout <= 0 or (args.duration is not None and args.duration < 0) or args.fps <= 0:
        print("stage 6A simulator error: timeout must be positive, duration non-negative, and fps positive", file=sys.stderr)
        return 2
    try:
        model = HouseModel(args.config)
        bridge = MqttBridge(model, args.host, args.port, args.timeout)
        bridge.connect()
        HouseSimulator(bridge, headless=args.headless, fps=args.fps).run(args.duration)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"stage 6A simulator error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stage 6A simulator stopped", flush=True)
    finally:
        if "bridge" in locals():
            bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
