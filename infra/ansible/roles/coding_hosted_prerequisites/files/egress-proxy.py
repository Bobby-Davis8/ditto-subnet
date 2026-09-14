#!/usr/bin/python3
"""Candidate egress proxy with an empty allowlist: refuse every request.

Hosted-v2 candidates reach inference and workspace tools only through the
source router at host.docker.internal, which the sandbox exempts via NO_PROXY.
There is no upstream connection code; the unit also denies other IP peers.
"""

import ipaddress
import re
import socketserver
import sys
import time

PRIVATE = tuple(
    ipaddress.IPv4Network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
MAX_HEAD = 8192
DEADLINE_SECONDS = 5.0
FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
NOT_ALLOWED = (
    b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)


def require(condition):
    if not condition:
        raise ValueError("egress proxy configuration rejected")


def listener(argv):
    require(len(argv) == 2 and re.fullmatch(r"[1-9][0-9]{3,4}", argv[1]) is not None)
    address, port = argv[0], int(argv[1])
    ip = ipaddress.IPv4Address(address)
    require(str(ip) == address and any(ip in network for network in PRIVATE))
    require(1024 <= port <= 65535)
    return address, port


def response(head):
    # The request line selects only which refusal is returned.
    return FORBIDDEN if head.startswith(b"CONNECT ") else NOT_ALLOWED


class Refusal(socketserver.BaseRequestHandler):
    def handle(self):
        deadline = time.monotonic() + DEADLINE_SECONDS
        head = b""
        try:
            while b"\r\n\r\n" not in head and len(head) < MAX_HEAD:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.request.settimeout(remaining)
                chunk = self.request.recv(MAX_HEAD - len(head))
                if not chunk:
                    return
                head += chunk
            self.request.settimeout(DEADLINE_SECONDS)
            self.request.sendall(response(head))
        except OSError:
            return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        """Never log candidate-controlled request data."""


def main(argv):
    with Server(listener(argv), Refusal) as server:
        server.serve_forever()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, OSError):
        print("coding hosted egress proxy unavailable", file=sys.stderr)
        raise SystemExit(1) from None
