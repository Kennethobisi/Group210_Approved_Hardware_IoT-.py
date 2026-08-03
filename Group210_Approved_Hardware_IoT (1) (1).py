#!/usr/bin/env python3
"""
Room Comfort Monitoring + Security Monitoring
Approved hardware version — no extra sensors or actuators

BCM GPIO wiring
---------------
DHT11 DATA                     GPIO4
HC-SR04 TRIG / ECHO             GPIO5 / GPIO6 (ECHO through 1 kΩ / 2 kΩ divider)
SG90 vent servo                 GPIO12
Active buzzer module IN         GPIO16
Security alarm LED              GPIO17 through 330 Ω
Vent-status LED                 GPIO22 through 330 Ω

The program publishes to MQTT and writes measurements directly to InfluxDB.
Grafana reads the InfluxDB "climate" measurement to show temperature,
humidity, vent status and security state.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from typing import Optional

import RPi.GPIO as GPIO
import adafruit_dht
import board
import paho.mqtt.client as mqtt
from influxdb import InfluxDBClient


# -------------------- change only these settings if needed ----------------
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DATABASE = "SRH_ES_iot"
ROOM = "room1"

VENT_OPEN_TEMPERATURE = 26.0
VENT_CLOSE_TEMPERATURE = 24.0
PRESENCE_DISTANCE_CM = 80.0
PRESENCE_CONFIRMATIONS = 3
SENSOR_INTERVAL_SECONDS = 1.0
DATABASE_INTERVAL_SECONDS = 5.0

# BCM GPIO numbers. Do not use physical pin numbers here.
DHT_PIN = 4
ULTRASONIC_TRIG_PIN = 5
ULTRASONIC_ECHO_PIN = 6
VENT_SERVO_PIN = 12
BUZZER_PIN = 16
SECURITY_LED_PIN = 17
VENT_STATUS_LED_PIN = 22


@dataclass
class SystemState:
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    distance_cm: Optional[float] = None
    vent_open: bool = False
    mode: str = "AWAY"  # HOME or AWAY; set from MQTT dashboard topics.
    alarm: bool = False
    presence_count: int = 0
    acknowledged: bool = False

    @property
    def security_state(self) -> str:
        if self.alarm:
            return "ALARM"
        return "ARMED" if self.mode == "AWAY" else "HOME"


class RoomComfortSecurityController:
    def __init__(self) -> None:
        self.state = SystemState()
        self.running = True
        self.last_database_write = 0.0

        self._setup_gpio()
        self.dht = adafruit_dht.DHT11(getattr(board, f"D{DHT_PIN}"), use_pulseio=False)
        self.influx = self._setup_influx()
        self.mqtt = self._setup_mqtt()

        self._set_alarm_outputs(False)
        self._set_vent(False, force=True)

    # --------------------------- hardware setup --------------------------
    def _setup_gpio(self) -> None:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(ULTRASONIC_TRIG_PIN, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(ULTRASONIC_ECHO_PIN, GPIO.IN)
        GPIO.setup(VENT_SERVO_PIN, GPIO.OUT)
        GPIO.setup(BUZZER_PIN, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(SECURITY_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(VENT_STATUS_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
        self.servo_pwm = GPIO.PWM(VENT_SERVO_PIN, 50)
        self.servo_pwm.start(0)

    def _setup_influx(self) -> Optional[InfluxDBClient]:
        try:
            client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT)
            client.create_database(INFLUX_DATABASE)
            client.switch_database(INFLUX_DATABASE)
            print(f"[INFLUX] Using database {INFLUX_DATABASE}")
            return client
        except Exception as error:
            print(f"[INFLUX] Not available: {error}")
            return None

    def _setup_mqtt(self):
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:  # Compatible with paho-mqtt 1.x.
            client = mqtt.Client()
        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, 60)
            client.loop_start()
            print(f"[MQTT] Connecting to {MQTT_BROKER}:{MQTT_PORT}")
        except Exception as error:
            print(f"[MQTT] Broker unavailable: {error}")
        return client

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "value", reason_code) == 0:
            print("[MQTT] Connected")
            client.subscribe(f"home/{ROOM}/security/mode")
            client.subscribe(f"home/{ROOM}/security/ack")
        else:
            print(f"[MQTT] Connection failed: {reason_code}")

    def _on_mqtt_message(self, client, userdata, message) -> None:
        payload = message.payload.decode(errors="ignore").strip().upper()
        if message.topic == f"home/{ROOM}/security/mode" and payload in {"HOME", "AWAY"}:
            self.state.mode = payload
            self.state.alarm = False
            self.state.presence_count = 0
            self.state.acknowledged = payload == "HOME"
            self._set_alarm_outputs(False)
            print(f"[SECURITY] Mode set to {payload}")
        elif message.topic == f"home/{ROOM}/security/ack" and payload in {"1", "ACK", "CLEAR"}:
            self.state.alarm = False
            self.state.acknowledged = True
            self._set_alarm_outputs(False)
            print("[SECURITY] Alarm acknowledged")

    # ------------------------------ sensors ------------------------------
    def _read_dht(self) -> None:
        try:
            temperature = self.dht.temperature
            humidity = self.dht.humidity
            if temperature is not None and humidity is not None:
                self.state.temperature = round(float(temperature), 1)
                self.state.humidity = round(float(humidity), 1)
        except RuntimeError:
            # DHT11 can occasionally miss a reading; keep the last valid value.
            pass
        except Exception as error:
            print(f"[DHT11] Read error: {error}")

    @staticmethod
    def _measure_distance_cm() -> Optional[float]:
        GPIO.output(ULTRASONIC_TRIG_PIN, GPIO.LOW)
        time.sleep(0.0002)
        GPIO.output(ULTRASONIC_TRIG_PIN, GPIO.HIGH)
        time.sleep(0.00001)
        GPIO.output(ULTRASONIC_TRIG_PIN, GPIO.LOW)

        timeout = time.monotonic() + 0.04
        while GPIO.input(ULTRASONIC_ECHO_PIN) == GPIO.LOW:
            if time.monotonic() > timeout:
                return None
        pulse_start = time.monotonic()

        timeout = time.monotonic() + 0.04
        while GPIO.input(ULTRASONIC_ECHO_PIN) == GPIO.HIGH:
            if time.monotonic() > timeout:
                return None
        pulse_end = time.monotonic()
        return round((pulse_end - pulse_start) * 17150.0, 1)

    # ----------------------------- outputs -------------------------------
    @staticmethod
    def _servo_duty_cycle(angle: float) -> float:
        # SG90 approximation at 50 Hz: 0° -> 2.5%, 180° -> 12.5%.
        return 2.5 + (angle / 180.0) * 10.0

    def _set_vent(self, open_vent: bool, force: bool = False) -> None:
        if not force and self.state.vent_open == open_vent:
            return
        angle = 70.0 if open_vent else 0.0
        self.servo_pwm.ChangeDutyCycle(self._servo_duty_cycle(angle))
        time.sleep(0.45)
        self.servo_pwm.ChangeDutyCycle(0)
        self.state.vent_open = open_vent
        GPIO.output(VENT_STATUS_LED_PIN, GPIO.HIGH if open_vent else GPIO.LOW)
        print(f"[VENT] {'OPEN' if open_vent else 'CLOSED'}")

    @staticmethod
    def _set_alarm_outputs(active: bool) -> None:
        GPIO.output(SECURITY_LED_PIN, GPIO.HIGH if active else GPIO.LOW)
        GPIO.output(BUZZER_PIN, GPIO.HIGH if active else GPIO.LOW)

    # -------------------------- control logic ----------------------------
    def _update_vent_control(self) -> None:
        temperature = self.state.temperature
        if temperature is None:
            return
        if temperature >= VENT_OPEN_TEMPERATURE:
            self._set_vent(True)
        elif temperature <= VENT_CLOSE_TEMPERATURE:
            self._set_vent(False)

    def _update_security_control(self) -> None:
        nearby = (
            self.state.distance_cm is not None
            and self.state.distance_cm <= PRESENCE_DISTANCE_CM
        )

        if self.state.mode == "AWAY":
            if nearby:
                self.state.presence_count += 1
            else:
                self.state.presence_count = 0
                self.state.acknowledged = False  # clear distance rearms the alarm

            if (
                self.state.presence_count >= PRESENCE_CONFIRMATIONS
                and not self.state.acknowledged
                and not self.state.alarm
            ):
                self.state.alarm = True
                self._set_alarm_outputs(True)
                print("[SECURITY] Ultrasonic presence alarm")
        else:
            self.state.presence_count = 0
            self.state.alarm = False
            self._set_alarm_outputs(False)

    # -------------------------- MQTT / InfluxDB --------------------------
    def _publish(self, topic_suffix: str, value) -> None:
        topic = f"home/{ROOM}/{topic_suffix}"
        try:
            self.mqtt.publish(topic, str(value), qos=0, retain=True)
        except Exception as error:
            print(f"[MQTT] Publish error for {topic}: {error}")

    def _write_influx(self, sensor: str, fields: dict) -> None:
        if self.influx is None:
            return
        point = {
            "measurement": "climate",
            "tags": {"room": ROOM, "sensor": sensor},
            "fields": fields,
        }
        try:
            self.influx.write_points([point])
        except Exception as error:
            print(f"[INFLUX] Write error: {error}")

    @staticmethod
    def _security_code(state: str) -> int:
        return {"HOME": 0, "ARMED": 1, "ALARM": 2}.get(state, -1)

    def _publish_and_log(self, now: float) -> None:
        security = self.state.security_state
        vent = "OPEN" if self.state.vent_open else "CLOSED"
        if self.state.temperature is not None:
            self._publish("temperature", self.state.temperature)
        if self.state.humidity is not None:
            self._publish("humidity", self.state.humidity)
        if self.state.distance_cm is not None:
            self._publish("security/distance_cm", self.state.distance_cm)
        self._publish("vent/status", vent)
        self._publish("security/state", security)
        self._publish("security/mode/current", self.state.mode)

        if now - self.last_database_write < DATABASE_INTERVAL_SECONDS:
            return
        self.last_database_write = now
        if self.state.temperature is not None and self.state.humidity is not None:
            self._write_influx(
                "dht11",
                {"temperature": self.state.temperature, "humidity": self.state.humidity},
            )
        if self.state.distance_cm is not None:
            self._write_influx("ultrasonic", {"distance_cm": self.state.distance_cm})
        self._write_influx("vent", {"state": vent, "state_code": int(self.state.vent_open)})
        self._write_influx(
            "security",
            {"state": security, "state_code": self._security_code(security)},
        )

    def run(self) -> None:
        print("[SYSTEM] Started. Press Ctrl+C to stop safely.")
        while self.running:
            start = time.monotonic()
            self._read_dht()
            self.state.distance_cm = self._measure_distance_cm()
            self._update_vent_control()
            self._update_security_control()
            self._publish_and_log(start)
            time.sleep(max(0.0, SENSOR_INTERVAL_SECONDS - (time.monotonic() - start)))

    def stop(self) -> None:
        self.running = False
        try:
            self._set_alarm_outputs(False)
            GPIO.output(VENT_STATUS_LED_PIN, GPIO.LOW)
            self._set_vent(False)
            self.servo_pwm.stop()
            self.dht.exit()
            self.mqtt.loop_stop()
            self.mqtt.disconnect()
        finally:
            GPIO.cleanup()
            print("[SYSTEM] Outputs switched off and GPIO cleaned up.")


def main() -> None:
    controller = RoomComfortSecurityController()

    def shutdown(signum, frame):
        controller.running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        controller.run()
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
