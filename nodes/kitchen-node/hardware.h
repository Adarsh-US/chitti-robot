#pragma once

#include <Arduino.h>

namespace kitchen_hardware {

constexpr uint8_t DHT_PIN = 15;
constexpr uint8_t FAN_LED_PIN = 2;

void begin();
float readTemperatureC();
void setFan(bool enabled);
bool fanIsOn();

}  // namespace kitchen_hardware
