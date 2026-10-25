#pragma once

#include <Arduino.h>

namespace kitchen_messages {

constexpr const char* ROOM = "kitchen";
constexpr const char* TEMP_TOPIC = "house/kitchen/temp";
constexpr const char* FAN_TOPIC = "house/kitchen/fan";

String temperaturePayload(float temperatureC, double timestamp);
String fanPayload(bool enabled, double timestamp);

// Accepts {"command":"fan_on"|"fan_off"} with an optional room field.
// A present room must be exactly "kitchen".
bool parseFanCommand(const String& input, bool& enabled, double& timestamp);

}  // namespace kitchen_messages
