#!/usr/bin/env python3
"""Transfer one file to an already logged-in serial shell via base64."""

import argparse
import base64
import fcntl
import os
import select
import struct
import sys
import termios
import time


def drain(fd: int, seconds: float) -> bytes:
    deadline = time.monotonic() + seconds
    output = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], min(0.1, deadline - time.monotonic()))
        if ready:
            output.extend(os.read(fd, 65536))
    return bytes(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--port", default="/dev/cu.usbserial-1340")
    args = parser.parse_args()

    payload = base64.b64encode(open(args.source, "rb").read())
    fd = os.open(args.port, os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    # Darwin requires IOSSIOSPEED for USB serial adapters; tcsetattr alone can
    # silently leave them at 9600. speed_t is an unsigned long on 64-bit macOS.
    if sys.platform == "darwin":
        fcntl.ioctl(fd, 0x80085402, struct.pack("L", 115200))

    command = (
        b"\nstty -icanon -echo min 1 time 0\n"
        + b"head -c "
        + str(len(payload)).encode()
        + b" | base64 -d > "
        + args.destination.encode()
        + b"\n"
    )
    os.write(fd, command)
    drain(fd, 0.5)
    sent = 0
    while sent < len(payload):
        _, writable, _ = select.select([], [fd], [], 1.0)
        if not writable:
            continue
        sent += os.write(fd, payload[sent : sent + 65536])
        drain(fd, 0.0)
        print(f"\r{sent}/{len(payload)}", end="", file=sys.stderr, flush=True)
    termios.tcdrain(fd)
    time.sleep(1.0)
    os.write(
        fd,
        b"stty icanon echo\nsha256sum " + args.destination.encode() + b"\n",
    )
    termios.tcdrain(fd)
    output = drain(fd, 3)
    os.close(fd)
    print(file=sys.stderr)
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
