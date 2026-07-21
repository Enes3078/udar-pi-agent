#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from datetime import datetime

try:
    from gpiozero import DigitalInputDevice
except ImportError as exc:
    raise SystemExit("python3-gpiozero gerekli. Kur: sudo apt install python3-gpiozero") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="UDAR GPIO pulse/duration diagnostic")
    parser.add_argument("--pin", type=int, default=27, help="BCM GPIO pin. Fiziksel pin 13 icin 27.")
    parser.add_argument("--pull-up", action="store_true", help="GPIO internal pull-up kullan.")
    parser.add_argument("--edge", choices=["rising", "falling", "both"], default="rising")
    parser.add_argument("--poll", type=float, default=0.002, help="Okuma araligi saniye.")
    parser.add_argument("--debounce", type=float, default=0.005, help="Tekrar sayim filtresi saniye.")
    args = parser.parse_args()

    pin = DigitalInputDevice(args.pin, pull_up=args.pull_up)
    last = bool(pin.value)
    count = 0
    last_counted = 0.0
    high_started = time.monotonic() if last else None

    print(
        f"GPIO{args.pin} izleniyor. edge={args.edge} initial={int(last)} poll={args.poll}s debounce={args.debounce}s. Ctrl+C ile cik.",
        flush=True,
    )

    while True:
        value = bool(pin.value)
        if value == last:
            time.sleep(args.poll)
            continue

        now = time.monotonic()
        is_rising = (not last) and value
        is_falling = last and (not value)
        edge = "rising" if is_rising else "falling"

        if is_rising:
            high_started = now
        elif is_falling and high_started is not None:
            print(f"  high sure: {now - high_started:.4f} sn", flush=True)
            high_started = None

        last = value
        should_count = args.edge == "both" or args.edge == edge
        counted = False
        if should_count and now - last_counted >= args.debounce:
            count += 1
            last_counted = now
            counted = True

        print(
            f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] value={int(value)} edge={edge} count={count}{' SAYILDI' if counted else ''}",
            flush=True,
        )
        time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())

