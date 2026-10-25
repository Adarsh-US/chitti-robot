#include "messages.h"

#include <ArduinoJson.h>
#include <math.h>

namespace kitchen_messages {

String temperaturePayload(float temperatureC, double timestamp) {
  JsonDocument document;
  document["ts"] = timestamp;
  document["room"] = ROOM;
  document["temp"] = temperatureC;
  String payload;
  serializeJson(document, payload);
  return payload;
}

String fanPayload(bool enabled, double timestamp) {
  JsonDocument document;
  document["ts"] = timestamp;
  document["room"] = ROOM;
  document["fan"] = enabled;
  String payload;
  serializeJson(document, payload);
  return payload;
}

bool parseFanCommand(const String& input, bool& enabled, double& timestamp) {
  JsonDocument document;
  if (deserializeJson(document, input)) {
    return false;
  }

  const char* command = document["command"] | "";
  const char* room = document["room"] | ROOM;
  if (strcmp(room, ROOM) != 0) {
    return false;
  }

  if (strcmp(command, "fan_on") == 0) {
    enabled = true;
  } else if (strcmp(command, "fan_off") == 0) {
    enabled = false;
  } else {
    return false;
  }

  const JsonVariantConst timestampValue = document["ts"];
  if (timestampValue.isNull()) {
    return false;
  }
  timestamp = timestampValue.as<double>();
  if (!isfinite(timestamp)) {
    return false;
  }
  return true;
}

}  // namespace kitchen_messages
