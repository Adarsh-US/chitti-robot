#pragma once

#include <Arduino.h>

using CommandHandler = void (*)(const String& commandJson);

void transportBegin(CommandHandler handler);
void transportLoop();
void transportPublish(const char* topic, const String& json);
