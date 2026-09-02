"""Low-frequency end-to-end Xray -> Mihomo -> egress IP probe."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time
from pathlib import Path


STATUS = Path(os.environ.get("PROBE_STATUS_PATH", "/status/status.json"))
INTERVAL = int(os.environ.get("PROBE_INTERVAL_SECONDS", "300"))
EXPECTED_IP = os.environ["PROBE_EXPECTED_EGRESS_IP"]


def socks_connect(proxy_host: str, proxy_port: int, target: str, port: int) -> socket.socket:
    sock = socket.create_connection((proxy_host, proxy_port), timeout=15)
    sock.sendall(b"\x05\x01\x00")
    if sock.recv(2) != b"\x05\x00":
        raise RuntimeError("SOCKS5 negotiation failed")
    name = target.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    reply = sock.recv(4)
    if len(reply) != 4 or reply[1] != 0:
        raise RuntimeError(f"SOCKS5 connect failed: {reply!r}")
    atyp = reply[3]
    sizes = {1: 4, 4: 16}
    size = sock.recv(1)[0] if atyp == 3 else sizes[atyp]
    sock.recv(size + 2)
    return sock


def probe() -> tuple[bool, str, str]:
    db = sqlite3.connect("/code/db.sqlite3")
    row = db.execute("select settings from proxies where user_id=1").fetchone()
    if not row:
        raise RuntimeError("probe user not found")
    password = json.loads(row[0])["password"]
    config = {
        "log": {"loglevel": "none"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 10999,
                "protocol": "socks",
                "settings": {"udp": False},
            }
        ],
        "outbounds": [
            {
                "protocol": "shadowsocks",
                "tag": "probe",
                "settings": {
                    "servers": [
                        {
                            "address": "127.0.0.1",
                            "port": 1080,
                            "method": "chacha20-ietf-poly1305",
                            "password": password,
                        }
                    ]
                },
            }
        ],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(config, handle)
        config_path = handle.name
    process = subprocess.Popen(
        ["/usr/local/bin/xray", "run", "-c", config_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2)
        raw = socks_connect("127.0.0.1", 10999, "api.ipify.org", 443)
        tls = ssl.create_default_context().wrap_socket(raw, server_hostname="api.ipify.org")
        tls.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
        body = b""
        while chunk := tls.recv(4096):
            body += chunk
        ip = body.split(b"\r\n\r\n", 1)[-1].decode().strip()
        return ip == EXPECTED_IP, ip, "ok" if ip == EXPECTED_IP else "unexpected egress IP"
    finally:
        process.terminate()
        process.wait(timeout=5)
        Path(config_path).unlink(missing_ok=True)


def main() -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            healthy, observed, detail = probe()
        except Exception as exc:  # status endpoint turns this into an alert
            healthy, observed, detail = False, "", type(exc).__name__
        STATUS.write_text(
            json.dumps(
                {
                    "healthy": healthy,
                    "observed_ip": observed,
                    "detail": detail,
                    "checked_at": int(time.time()),
                }
            )
            + "\n"
        )
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
