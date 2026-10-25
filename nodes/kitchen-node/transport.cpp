#include "transport.h"

#include "messages.h"

#if USE_MQTT
#include "config.h"
#include <PubSubClient.h>
#include <WiFi.h>

namespace {
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
CommandHandler commandHandler = nullptr;
unsigned long lastMqttAttempt = 0;

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  if (strcmp(topic, kitchen_messages::FAN_TOPIC) != 0 || commandHandler == nullptr) {
    return;
  }

  String command;
  command.reserve(length + 1);
  for (unsigned int i = 0; i < length; ++i) {
    command += static_cast<char>(payload[i]);
  }
  commandHandler(command);
}

bool connectMqtt() {
  if (mqttClient.connected()) {
    return true;
  }
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  const bool connected = mqttClient.connect(
      KITCHEN_MQTT_CLIENT_ID, KITCHEN_MQTT_USER, KITCHEN_MQTT_PASSWORD);
  if (connected) {
    mqttClient.subscribe(kitchen_messages::FAN_TOPIC);
  }
  return connected;
}
}  // namespace

void transportBegin(CommandHandler handler) {
  commandHandler = handler;
  mqttClient.setServer(KITCHEN_MQTT_HOST, KITCHEN_MQTT_PORT);
  mqttClient.setCallback(mqttCallback);
  WiFi.mode(WIFI_STA);
  WiFi.begin(KITCHEN_WIFI_SSID, KITCHEN_WIFI_PASSWORD);
}

void transportLoop() {
  if (!mqttClient.connected()) {
    const unsigned long now = millis();
    if (now - lastMqttAttempt >= 5000 || lastMqttAttempt == 0) {
      lastMqttAttempt = now;
      connectMqtt();
    }
    return;
  }
  mqttClient.loop();
}

void transportPublish(const char* topic, const String& json) {
  if (mqttClient.connected()) {
    mqttClient.publish(topic, json.c_str());
  }
}

#else

namespace {
CommandHandler commandHandler = nullptr;
String inputLine;
}

void transportBegin(CommandHandler handler) {
  commandHandler = handler;
  Serial.begin(115200);
  inputLine.reserve(256);
}

void transportLoop() {
  while (Serial.available() > 0) {
    const char character = static_cast<char>(Serial.read());
    if (character == '\n' || character == '\r') {
      if (inputLine.length() > 0 && commandHandler != nullptr) {
        commandHandler(inputLine);
      }
      inputLine = "";
    } else {
      inputLine += character;
    }
  }
}

void transportPublish(const char* topic, const String& json) {
  Serial.print(topic);
  Serial.print(' ');
  Serial.println(json);
}

#endif
