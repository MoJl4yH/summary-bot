"""Проверка состояния для Docker HEALTHCHECK.

Основной процесс обновляет файл-«сердцебиение», пока работают long polling и
планировщик. Если файл устарел — процесс завис или упал.
"""
import os
import sys
import time

path = os.environ.get("HEARTBEAT_PATH", "/tmp/summary-bot.heartbeat")
max_age = float(os.environ.get("HEARTBEAT_MAX_AGE_SECONDS", "180"))

try:
    age = time.time() - os.stat(path).st_mtime
except FileNotFoundError:
    print("heartbeat отсутствует")
    sys.exit(1)

if age > max_age:
    print(f"heartbeat устарел: {age:.0f} с")
    sys.exit(1)
print(f"ok ({age:.0f} с)")
sys.exit(0)
