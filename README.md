# Jarvis Home — Stage 1 through Stage 6A

Stage 1 verifies local MQTT connectivity. Stage 2 adds the pygame house and
robot simulator. Stage 3 adds the Gemini-backed Jarvis brain; Stage 4 adds
local push-to-talk voice input; Stage 5 adds the Wokwi kitchen node; Stage 6A
makes the house multi-floor and config-driven. Stage 6B is not implemented.

## Setup

The examples expect Python 3.10+ and a local Mosquitto broker.

```sh
cd /path/to/jarvis-home
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Start Mosquitto on its default listener (`localhost:1883`):

```sh
python house_sim/test_pubsub.py --host localhost --port 1883
```

This publishes and subscribes to `house/test/temp` and exits successfully only
after receiving the matching JSON payload.

## Stage 6A: config-driven multi-floor house

`config/house.json` is the single source of truth for the two-floor layout,
room IDs, doors, stairs, charging dock, and floor temperature offsets. The
current rooms are:

- Ground: `living-room`, `kitchen`, `dining-room`, `hallway`, `study`
- First: `landing`, `bedroom`, `bedroom-2`, `bathroom`

Run the simulator as before; it loads the config automatically:

```sh
python -m house_sim.sim --host localhost --port 1883
```

For a headless check:

```sh
SDL_VIDEODRIVER=dummy python -m house_sim.sim \
  --headless --duration 10 --host localhost --port 1883
```

`move_to` follows configured door and stair routes. `robot/state` preserves
all previous fields and adds `floor`. When battery falls below 20%, the robot
routes to the hallway dock and reports `current_task` as `charging`. Furniture
metadata is present in the config for the later drawing stage but is not drawn
in Stage 6A.

## Stage 2: pygame house and robot simulator

The simulator retains the existing MQTT sensor contract while the Stage 6A
config supplies the expanded layout. It publishes temperature, motion, door,
light, and fan for every configured room every second.

Start a local Mosquitto broker on `localhost:1883`, then run the simulator:

```sh
python -m house_sim.sim --host localhost --port 1883
```

For a display-free run (useful in CI), the same MQTT loop runs with SDL's
dummy video driver:

```sh
python -m house_sim.sim --headless --duration 10 --host localhost --port 1883
```

Press `H` to heat/cook the robot's current room and `P` to add a temporary
person. `Esc` or `Q` quits the GUI. The simulator publishes once per second
on only these topics:

* `house/<room>/temp`
* `house/<room>/motion`
* `house/<room>/door`
* `house/<room>/light`
* `house/<room>/fan`
* `robot/state`

It subscribes to `robot/command`. Every payload is JSON and includes a numeric
`ts` field. Robot state has `x`, `y`, `room`, `battery`, and `current_task`.
Commands use this shape:

```json
{"ts": 1730000000.0, "command": "move_to", "room": "kitchen"}
{"ts": 1730000001.0, "command": "stop"}
{"ts": 1730000002.0, "command": "fan_on", "room": "kitchen"}
{"ts": 1730000003.0, "command": "fan_off", "room": "kitchen"}
{"ts": 1730000004.0, "command": "light_on", "room": "bedroom"}
{"ts": 1730000005.0, "command": "light_off", "room": "bedroom"}
```

`move_to` accepts the four exact room IDs. Fan and light commands affect the
the `room` field when present and default to the robot's current room.

## Topic and payload contract

All payloads are UTF-8 JSON objects. Every JSON payload has a numeric `ts`
field containing a Unix timestamp in seconds (fractional seconds are allowed).

| Direction | Topic | Payload |
| --- | --- | --- |
| House → consumers | `house/<room>/temp` | `{"ts": 1730000000.0, "room": "living-room", "temp": 21.0}` |
| House → consumers | `house/<room>/motion` | `{"ts": 1730000000.0, "room": "living-room", "motion": true}` |
| House → consumers | `house/<room>/door` | `{"ts": 1730000000.0, "room": "living-room", "door": true}` |
| House → consumers | `house/<room>/light` | `{"ts": 1730000000.0, "room": "living-room", "light": true}` |
| House → consumers | `house/<room>/fan` | `{"ts": 1730000000.0, "room": "living-room", "fan": false}` |
| House → consumers | `robot/state` | `{"ts": 1730000000.0, "x": 240.0, "y": 210.0, "room": "living-room", "floor": 0, "battery": 100.0, "current_task": "idle"}` |
| Consumers → robot | `robot/command` | `{"ts": 1730000000.0, "command": "move_to", "room": "kitchen"}` |

`<room>` is exactly one of the room IDs in `config/house.json`.
`temp` is a JSON number in degrees Celsius.

## Stage 3: Jarvis brain

Run the brain alongside the simulator:

```sh
python -m brain.jarvis --host localhost --port 1883
```

Set `GEMINI_API_KEY` for LLM decisions. The model defaults to
`gemini-3.5-flash-lite` and can be changed with `JARVIS_MODEL`. The brain still
starts without the key, logs that the LLM is unavailable when a request
arrives, and keeps its high-temperature fan safety rule active.

`JARVIS_TEST_LLM_RESPONSE` is a test-only hook. It is inactive unless you
explicitly set it, and is not used by normal runs.

## Stage 4: local voice input

Install the voice dependencies from `requirements.txt`, then start the local
push-to-talk listener. Press Enter once to start recording and Enter again to
stop; accepted English transcripts are printed and published to
`user/speech` as JSON.

```sh
python -m voice.listen --host localhost --port 1883
```

To inspect microphone indexes, use:

```sh
python -m voice.listen --list-devices
python -m voice.listen --device 2 --host localhost --port 1883
```

To transcribe a WAV file without opening the microphone:

```sh
python -m voice.listen --file /path/to/recording.wav --host localhost --port 1883
```

The default local model is faster-whisper `small` on CPU with int8 compute.
Use `--model MODEL` to select another local faster-whisper model. Speech stays
local; this stage does not use a cloud speech API.
