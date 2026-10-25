# Kitchen node — Stage 5

This is the Stage 5 ESP32 Arduino node for the exact room ID `kitchen`. The
hardware layer reads a DHT22 on GPIO 15 and drives the fan indicator LED on
GPIO 2. Temperature telemetry is published every two seconds.

## Message contract

The node publishes UTF-8 JSON lines/messages with numeric `ts` values:

* `house/kitchen/temp`: `{"ts":1730000000.0,"room":"kitchen","temp":24.00}`
* `house/kitchen/fan`: `{"ts":1730000000.0,"room":"kitchen","fan":true}`

`fan_on` and `fan_off` commands use JSON with an optional `room` (when
present it must be `kitchen`):

```json
{"ts":1730000000.0,"command":"fan_on","room":"kitchen"}
{"ts":1730000001.0,"command":"fan_off","room":"kitchen"}
```

## Build and run

The default build is a no-network serial transport that works in Wokwi. Build
it with the repository's Arduino CLI:

```sh
arduino-cli compile \
  --fqbn esp32:esp32:esp32 \
  --output-dir build .
wokwi-cli . --scenario temp_output.scenario.yaml
```

Serial telemetry and fan acknowledgements are printed exactly as
`<topic> <json>`. Send a command JSON line at 115200 baud; a fan acknowledgement
is a `house/kitchen/fan` line and the LED follows GPIO 2.

For MQTT, copy `config.example.h` to `config.h`, fill in the local credentials,
and build the opt-in variant. The MQTT transport is compile-tested but is not
needed for Wokwi:

```sh
cp config.example.h config.h
arduino-cli compile \
  --fqbn esp32:esp32:esp32 \
  --build-property build.extra_flags=-DUSE_MQTT=1 \
  --output-dir build-mqtt .
```

The MQTT transport connects WiFi, publishes the two contract topics, and
subscribes to `house/kitchen/fan` for command JSON. `config.h` is ignored and
must not be committed.

The Arduino CLI compile uses `build/` for the Wokwi firmware referenced by
`wokwi.toml`. On real hardware, copy `config.example.h` to `config.h`, fill in
the WiFi and broker values, compile the MQTT variant, and upload it to an ESP32
DevKit. The serial-only build remains the default for local simulation.
