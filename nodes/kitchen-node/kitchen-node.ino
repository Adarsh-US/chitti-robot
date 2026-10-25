#include <Arduino.h>
#include <time.h>

#include "hardware.h"
#include "messages.h"
#include "transport.h"

#ifndef USE_MQTT
#define USE_MQTT 0
#endif

namespace {
constexpr unsigned long TEMPERATURE_INTERVAL_MS = 2000;
unsigned long lastTemperatureRead = 0;

double messageTimestamp() {
  const time_t now = time(nullptr);
  if (now >= 100000) {
    return static_cast<double>(now);
  }
  // The serial/Wokwi transport has no clock service; retain a numeric,
  // monotonic timestamp until an MQTT build obtains wall-clock time.
  return static_cast<double>(millis()) / 1000.0;
}

void handleCommand(const String& commandJson) {
  bool enabled = false;
  double commandTimestamp = 0.0;
  if (!kitchen_messages::parseFanCommand(commandJson, enabled, commandTimestamp)) {
    return;
  }

  kitchen_hardware::setFan(enabled);
  // The state publication is also the serial acknowledgement and contains
  // the parseable kitchen fan boolean required by the node contract.
  transportPublish(kitchen_messages::FAN_TOPIC,
                   kitchen_messages::fanPayload(enabled, messageTimestamp()));
}

void publishTemperature() {
  const float temperatureC = kitchen_hardware::readTemperatureC();
  if (isnan(temperatureC)) {
    return;
  }
  transportPublish(kitchen_messages::TEMP_TOPIC,
                   kitchen_messages::temperaturePayload(temperatureC, messageTimestamp()));
}
}  // namespace

void setup() {
  kitchen_hardware::begin();
  transportBegin(handleCommand);
  lastTemperatureRead = millis();
}

void loop() {
  transportLoop();

  const unsigned long now = millis();
  if (now - lastTemperatureRead >= TEMPERATURE_INTERVAL_MS) {
    lastTemperatureRead = now;
    publishTemperature();
  }
}
