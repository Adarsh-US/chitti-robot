#include "hardware.h"

#include <DHT.h>

namespace {
DHT dht(kitchen_hardware::DHT_PIN, DHT22);
bool fanOn = false;
}  // namespace

namespace kitchen_hardware {

void begin() {
  pinMode(FAN_LED_PIN, OUTPUT);
  setFan(false);
  dht.begin();
}

float readTemperatureC() {
  return dht.readTemperature();
}

void setFan(bool enabled) {
  fanOn = enabled;
  digitalWrite(FAN_LED_PIN, fanOn ? HIGH : LOW);
}

bool fanIsOn() {
  return fanOn;
}

}  // namespace kitchen_hardware
