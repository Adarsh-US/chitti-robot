# Jarvis Home project instructions

## Room identifiers

Room IDs are exact MQTT topic segments. Use lowercase hyphenated IDs everywhere.
The configured IDs are:

- `living-room`
- `kitchen`
- `dining-room`
- `hallway`
- `study`
- `landing`
- `bedroom`
- `bedroom-2`
- `bathroom`

Do not introduce underscore or alternate room IDs.

## Project layout

Keep application code in the component folders:

- `house_sim/`
- `brain/`
- `voice/`
- `nodes/`

The project root may contain only `AGENTS.md`, `README.md`, and
`requirements.txt` as project files. Do not add prototype code or stage
implementation files directly to the root.

## Staged implementation

Implement only the stage explicitly requested by the user. Do not prototype or
start later stages until the user asks for them.

All components communicate through MQTT only; they must not call one another
directly. JSON MQTT payloads must include a numeric `ts` timestamp.

Fan and light commands act on the `room` field of the command (default: the
robot's current room). Only `move_to` and `stop` depend on the robot's position.

## Stage 3 contract

The simulator publishes `house/<room>/fan` for all four rooms with JSON
payloads that include `ts` and the fan state. Jarvis consumes that topic along
with the other house sensors.

Jarvis uses Google Gemini through the official `google-genai` SDK. Read the
Google AI Studio key from `GEMINI_API_KEY` and the model from `JARVIS_MODEL`;
never hard-code, print, or log the key. The optional
`JARVIS_TEST_LLM_RESPONSE` override is test-only and must only be used when it
is explicitly set.

## Stage 4 voice input

Voice input lives in `voice/` and communicates only through MQTT. Install the
dependencies and run the terminal push-to-talk listener with:

```sh
cd /path/to/jarvis-home
python -m pip install -r requirements.txt
python -m voice.listen --host localhost --port 1883
```

Press Enter to start recording and Enter again to stop. Use
`python -m voice.listen --list-devices` to list microphone indexes,
`--device INDEX` to select one, or `--file recording.wav` to transcribe a WAV
without microphone access. The listener uses the local faster-whisper `small`
model by default, does not use a cloud speech API, and publishes accepted
transcripts to `user/speech` as JSON with numeric `ts` and string `text` fields.

## Wokwi

- Keep `WOKWI_CLI_TOKEN` in the environment; never print or write it.
- Build firmware before running `wokwi-cli .`.
- Wokwi projects require `diagram.json` and `wokwi.toml` pointing to the built
  firmware.
- Verify behavior with a scenario file and inspect serial output.

## Stage 6A multi-floor house

`config/house.json` is the single source of truth for the house layout. It
defines floors 0 and 1, room IDs, room rectangles, doors, the hallway/landing
stairs, charging dock, furniture metadata, and per-floor temperature offsets.
The house simulator and Jarvis must load this file rather than maintaining
separate room allowlists. Keep the existing IDs `living-room`, `kitchen`,
`bedroom`, and `bathroom` unchanged; the complete current room set is:

- Ground floor: `living-room`, `kitchen`, `dining-room`, `hallway`, `study`
- First floor: `landing`, `bedroom`, `bedroom-2`, `bathroom`

`robot/state` retains `x`, `y`, `room`, `battery`, and `current_task` and adds
numeric `floor`. The existing five sensor topics continue to publish for every
configured room. Stage 6A includes routing, stairs, low-battery docking and
charging, but does not draw furniture. Do not start Stage 6B until requested.

## Stage 5 nodes

The Stage 5 kitchen node lives in `nodes/kitchen-node/` and uses room ID
exactly `kitchen`. Its hardware contract is a DHT22 on GPIO 15 and a fan LED
output on GPIO 2. It publishes numeric-`ts` JSON on:

- `house/kitchen/temp`: `{"ts": 1730000000.0, "room": "kitchen", "temp": 21.0}`
- `house/kitchen/fan`: `{"ts": 1730000000.0, "room": "kitchen", "fan": false}`

Fan commands are JSON `fan_on` or `fan_off` with an optional exact `room` value
of `kitchen`; serial transport accepts command lines and MQTT transport accepts
commands on `house/kitchen/fan`. The default build is serial-only for Wokwi.
Set `USE_MQTT` through the Arduino CLI build property for real hardware, using
the ignored `nodes/kitchen-node/config.h` copied from its example.

The Wokwi default is serial-only: build with
`arduino-cli compile --fqbn esp32:esp32:esp32 --output-dir build .` from
`nodes/kitchen-node/`, then run the scenarios with
`wokwi-cli . --scenario <file>`. For real hardware, copy
`config.example.h` to the ignored `config.h`, set WiFi and broker values, and
compile with `--build-property build.extra_flags=-DUSE_MQTT=1`. The node's
message contract is `house/kitchen/temp` with `ts`, `room`, and `temp`, and
`house/kitchen/fan` with `ts`, `room`, and `fan`; fan commands are
`fan_on`/`fan_off` JSON and the serial/MQTT transport carries them to the
hardware layer.
