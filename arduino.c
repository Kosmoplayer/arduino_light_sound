#include <Arduino.h>
#include <EEPROM.h>
#include <ArduinoJson.h>

#define SENSOR_PIN      A0
#define RELAY_PIN       7
#define RELAY_ON        HIGH
#define RELAY_OFF       LOW
#define BAUD_RATE       115200

// Структура конфигурации (сохраняется в EEPROM)
struct Config {
  uint16_t threshold = 300;
  uint16_t hysteresis = 30;
  uint8_t  N = 8;
  float    alpha = 0.15;
  uint16_t t_min = 20;
  uint16_t t_max = 150;
  uint16_t cooldown = 400;
  uint8_t  reserved[2];
} config;

volatile bool sampleReady = false;
volatile uint16_t rawAdc = 0;

uint16_t dcOffset = 512;
float envelope = 0.0;
float avgBuffer[16] = {0};
uint8_t avgIdx = 0;
uint16_t avgSum = 0;

enum SysState { IDLE, DETECTING, VALIDATING, COOLDOWN };
SysState state = IDLE;

unsigned long stateStartMs = 0;
uint16_t impulsePeak = 0;
float impulseSum = 0;
uint16_t impulseCount = 0;
unsigned long lastTriggerMs = 0;
bool relayState = false;

// Буфер для приёма JSON по Serial
char serialBuffer[128] = {0};
uint8_t serialIdx = 0;

void setupTimer1() {
  cli();
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1 = 0;
  // Prescaler 64 -> 250 kHz. Для 1000 Гц: 250000 / 250 = 1000
  OCR1A = 249; 
  TCCR1B |= (1 << WGM12);
  TCCR1B |= (1 << CS11) | (1 << CS10); // Prescaler 64
  TIMSK1 |= (1 << OCIE1A);
  sei();
}

ISR(TIMER1_COMPA_vect) {
  rawAdc = analogRead(SENSOR_PIN);
  sampleReady = true;
}

void calibrateDC() {
  uint32_t sum = 0;
  const uint8_t samples = 100;
  for (uint8_t i = 0; i < samples; i++) {
    sum += analogRead(SENSOR_PIN);
    delay(2);
  }
  dcOffset = sum / samples;
  // Инициализация огибающей
  envelope = 0.0;
  for (uint8_t i = 0; i < config.N; i++) avgBuffer[i] = dcOffset;
}

void processSample() {
  int16_t acSignal = (int16_t)rawAdc - dcOffset;
  
  avgSum -= avgBuffer[avgIdx];
  avgBuffer[avgIdx] = acSignal;
  avgSum += acSignal;
  float filtered = avgSum / config.N;
  avgIdx = (avgIdx + 1) % config.N;

  float absVal = abs(filtered);
  envelope = config.alpha * absVal + (1.0 - config.alpha) * envelope;

  uint16_t T_high = config.threshold + config.hysteresis;
  uint16_t T_low  = config.threshold - config.hysteresis;
  uint16_t envInt = (uint16_t)envelope;

  switch (state) {
    case IDLE:
      if (envInt > T_high) {
        state = DETECTING;
        stateStartMs = millis();
        impulsePeak = 0;
        impulseSum = 0;
        impulseCount = 0;
      }
      break;

    case DETECTING:
      impulseCount++;
      impulseSum += envInt;
      if (envInt > impulsePeak) impulsePeak = envInt;

      if (envInt < T_low) {
        state = VALIDATING;
      }
      break;

    case VALIDATING: {
      uint16_t duration = millis() - stateStartMs;
      
      // Проверка длительности
      if (duration < config.t_min || duration > config.t_max) {
        state = IDLE;
        break;
      }

      // Проверка качества волны (crest factor > 2.5)
      float avgEnv = impulseSum / impulseCount;
      float crestFactor = (avgEnv > 0.01) ? (impulsePeak / avgEnv) : 0.0;

      if (millis() - lastTriggerMs < config.cooldown) {
        state = IDLE;
        break;
      }

      if (crestFactor > 2.5) {
        relayState = !relayState;
        digitalWrite(RELAY_PIN, relayState ? RELAY_ON : RELAY_OFF);
        lastTriggerMs = millis();
        state = COOLDOWN;
      } else {
        state = IDLE;
      }
      break;
    }

    case COOLDOWN:
      if (millis() - lastTriggerMs >= config.cooldown) {
        state = IDLE;
      }
      break;
  }
}

void sendTelemetry() {
  // Отправка каждые ~20мс для визуализации в PyQt6 без перегрузки UART
  static unsigned long lastTx = 0;
  if (millis() - lastTx < 20) return;
  lastTx = millis();

  // Формат: {"a":ADC,"e":ENV,"s":STATE,"r":RELAY}
  Serial.print("{\"a\":");
  Serial.print(rawAdc);
  Serial.print(",\"e\":");
  Serial.print((uint16_t)envelope);
  Serial.print(",\"s\":");
  Serial.print((uint8_t)state);
  Serial.print(",\"r\":");
  Serial.print(relayState ? 1 : 0);
  Serial.println("}");
}

void parseCommand(const char* json) {
  StaticJsonDocument<256> doc;
  DeserializationError error = deserializeJson(doc, json);
  if (error) return;

  const char* cmd = doc["cmd"];
  if (!cmd) return;

  if (strcmp(cmd, "config") == 0) {
    if (doc.containsKey("T")) config.threshold = doc["T"].as<uint16_t>();
    if (doc.containsKey("delta")) config.hysteresis = doc["delta"].as<uint16_t>();
    if (doc.containsKey("N")) config.N = constrain(doc["N"].as<uint8_t>(), 2, 16);
    if (doc.containsKey("alpha")) config.alpha = doc["alpha"].as<float>();
    if (doc.containsKey("t_min")) config.t_min = doc["t_min"].as<uint16_t>();
    if (doc.containsKey("t_max")) config.t_max = doc["t_max"].as<uint16_t>();
    if (doc.containsKey("cooldown")) config.cooldown = doc["cooldown"].as<uint16_t>();

    // Пересчитываем буфер под новый N
    for (uint8_t i = 0; i < config.N; i++) avgBuffer[i] = dcOffset;
    avgIdx = 0; avgSum = 0;

    // Сохранение в EEPROM
    EEPROM.put(0, config);
    Serial.println("{\"status\":\"config_saved\"}");
  } 
  else if (strcmp(cmd, "manual") == 0) {
    const char* action = doc["action"];
    if (strcmp(action, "on") == 0) relayState = true;
    else if (strcmp(action, "off") == 0) relayState = false;
    else if (strcmp(action, "toggle") == 0) relayState = !relayState;
    
    digitalWrite(RELAY_PIN, relayState ? RELAY_ON : RELAY_OFF);
    lastTriggerMs = millis(); // Сбрасываем cooldown
    Serial.println("{\"status\":\"manual_updated\",\"r\":");
    Serial.print(relayState ? 1 : 0);
    Serial.println("}");
  }
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      serialBuffer[serialIdx] = '\0';
      if (serialIdx > 5) parseCommand(serialBuffer);
      serialIdx = 0;
    } else if (c >= 32 && serialIdx < sizeof(serialBuffer) - 1) {
      serialBuffer[serialIdx++] = c;
    }
  }
}

void setup() {
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, RELAY_OFF);
  
  Serial.begin(BAUD_RATE);
  delay(500); 

  // Загрузка конфигурации из EEPROM
  EEPROM.get(0, config);
  if (config.threshold > 1024) { // Проверка на пустую/битую EEPROM
    config = Config();
    EEPROM.put(0, config);
  }

  Serial.print("{\"status\":\"init\",\"threshold\":");
  Serial.print(config.threshold);
  Serial.println("}");

  calibrateDC();
  setupTimer1();
}

void loop() {
  if (sampleReady) {
    sampleReady = false;
    processSample();
  }

  handleSerial();
  sendTelemetry();
}
