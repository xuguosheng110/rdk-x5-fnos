#!/usr/bin/env python3
"""Find the freshly booted fnOS board on the local /24 (read-only probes).

Checks TCP 80/8000/443, then fetches the HTTP title for candidates.  Excludes
the Mac's own address.  No credentials, no writes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess


async def tcp(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True
    except Exception:
        return False


def http_title(host: str, port: int) -> str:
    scheme = "https" if port == 443 else "http"
    try:
        out = subprocess.run(
            ["curl", "-k", "-sS", "--max-time", "4", "-o", "-", f"{scheme}://{host}:{port}/"],
            capture_output=True, text=True, timeout=8, check=False).stdout
        start = out.lower().find("<title>")
        end = out.lower().find("</title>", start + 7)
        return out[start + 7:end].strip()[:120] if start >= 0 and end > start else ""
    except Exception:
        return ""


async def main_async(net: str, ports: list[int]) -> int:
    own = set()
    for line in subprocess.run(["ifconfig"], capture_output=True, text=True).stdout.splitlines():
        if "inet " in line:
            own.add(line.split("inet ")[1].split()[0])
    hosts = [f"{net}.{i}" for i in range(1, 255)]
    results = []
    sem = asyncio.Semaphore(64)

    async def probe(host):
        async with sem:
            open_ports = [p for p in ports if await tcp(host, p)]
        if open_ports:
            return {"host": host, "ports": open_ports,
                    "own": host in own,
                    "titles": {str(p): http_title(host, p) for p in open_ports}}
        return None

    for item in await asyncio.gather(*(probe(h) for h in hosts)):
        if item:
            results.append(item)
    print(json.dumps({"schema": "fnos-lan-find/v1", "net": net,
                      "candidates": results}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", default="192.168.3")
    ap.add_argument("--ports", default="80,8000,443")
    args = ap.parse_args()
    return asyncio.run(main_async(args.net, [int(p) for p in args.ports.split(",")]))


if __name__ == "__main__":
    raise SystemExit(main())
