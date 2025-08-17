import os
import random
import time
from prometheus_client import start_http_server, Gauge

temperature_g = Gauge("sensor_temperature_celsius", "Temperature from sensor", ["location"])

def collect_loop():
    locations = ["chiller_in", "chiller_out", "room"]
    while True:
        for loc in locations:
            # TODO: replace with real sensor reading
            reading = 18.0 + random.random() * 5.0
            temperature_g.labels(location=loc).set(reading)
        time.sleep(5)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    start_http_server(port)
    collect_loop()