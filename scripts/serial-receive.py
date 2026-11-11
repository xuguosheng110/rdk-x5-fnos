#!/usr/bin/env python3
"""Receive one file from an already logged-in serial shell via base64."""

import argparse
import base64
import fcntl
import os
import select
import struct
import sys
import termios
import time


BEGIN = b"__CODEX_SERIAL_BEGIN__"
END = b"__CODEX_SERIAL_END__"


def read_until(fd: int, marker: bytes, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    last_report = 0
    while marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {marker.decode()}")
        ready, _, _ = select.select([fd], [], [], min(1.0, remaining))
        if ready:
            output.extend(os.read(fd, 65536))
            if len(output) - last_report >= 16384:
                print(f"received {len(output)} serial bytes", file=sys.stderr, flush=True)
                last_report = len(output)
    print(f"received {len(output)} serial bytes", file=sys.stderr)
    return bytes(output)


def configure(fd: int) -> None:
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    if sys.platform == "darwin":
        fcntl.ioctl(fd, 0x80085402, struct.pack("L", 115200))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--port", default="/dev/cu.usbserial-1340")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    fd = os.open(args.port, os.O_RDWR | os.O_NOCTTY)
    try:
        configure(fd)
        os.write(fd, b"\nstty -echo\n")
        time.sleep(0.25)
        while select.select([fd], [], [], 0)[0]:
            os.read(fd, 65536)

        command = (
            b"printf '" + BEGIN + b"\\n'; base64 < " + args.source.encode()
            + b"; printf '" + END + b"\\n'\n"
        )
        os.write(fd, command)
        captured = read_until(fd, END, args.timeout)
        start = captured.find(BEGIN)
        stop = captured.find(END, start + len(BEGIN))
        if start < 0 or stop < 0:
            raise RuntimeError("serial framing markers were not captured")
        encoded = captured[start + len(BEGIN):stop]
        payload = base64.b64decode(b"".join(encoded.split()), validate=True)
        with open(args.destination, "wb") as output:
            output.write(payload)
        os.write(fd, b"stty echo\n")
    finally:
        os.close(fd)

    print(f"wrote {len(payload)} bytes to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
