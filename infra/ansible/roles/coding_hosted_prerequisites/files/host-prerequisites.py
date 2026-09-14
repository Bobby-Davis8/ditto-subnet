#!/usr/bin/python3
"""Check the fixed native-v2 host record. Read-only: no host state is changed."""

import ipaddress
import json
import os
import re
import runpy
import socket
import stat
import sys
from pathlib import Path

SCHEMA = "dittobench-coding-hosted-host-prerequisites-v2"
USER = "ditto-coding-hosted"
HOST_POLICY = Path("/usr/local/lib/ditto-coding-hosted/host-policy.py")
PORT_RANGE = Path("/proc/sys/net/ipv4/ip_local_port_range")
KEYS = {
    "schema",
    "shadow_only",
    "weight_eligible",
    "router_listen",
    "egress_network",
    "egress_proxy",
    "candidate_uid",
    "candidate_gid",
}
# Go net.IP.IsPrivate for IPv4; connectivity candidate_tcp uses the same set.
PRIVATE = tuple(
    ipaddress.IPv4Network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
PORT = re.compile(r"[1-9][0-9]{3,4}")
# codinghostedruntime identifier(): 1-128 of [a-z0-9_-], no leading dash.
NETWORK = re.compile(r"[a-z0-9_][a-z0-9_-]{0,127}")
# host-policy.py admits exactly one 65,536-ID range. Rootless Docker maps
# container ID 0 to the daemon account and IDs 1..65536 into that range.
SUBORDINATE_IDS = 65536


def require(condition):
    if not condition:
        raise ValueError("host prerequisites rejected")


def endpoint(text):
    """Canonical private IPv4 and unprivileged port; no names, IPv6 or wildcards."""
    require(type(text) is str and text.count(":") == 1)
    address, port = text.split(":")
    require(PORT.fullmatch(port) is not None and 1024 <= int(port) <= 65535)
    ip = ipaddress.IPv4Address(address)
    require(str(ip) == address and any(ip in network for network in PRIVATE))
    return address, int(port)


def settings(document):
    require(type(document) is dict and set(document) == KEYS)
    require(document["schema"] == SCHEMA)
    require(document["shadow_only"] is True and document["weight_eligible"] is False)
    router = endpoint(document["router_listen"])
    proxy = document["egress_proxy"]
    require(type(proxy) is str and proxy.startswith("http://"))
    proxy = endpoint(proxy.removeprefix("http://"))
    # The router address is also the sandbox host gateway; the refusing proxy
    # listens beside it so one candidate_tcp address covers both listeners.
    require(router[0] == proxy[0] and router[1] != proxy[1])
    network = document["egress_network"]
    require(type(network) is str and NETWORK.fullmatch(network) is not None)
    # The runtime creates and removes its own ditto-job-<id> bridge per start.
    require(not network.startswith("ditto-job-"))
    for key in ("candidate_uid", "candidate_gid"):
        value = document[key]
        require(type(value) is int and 1 <= value <= SUBORDINATE_IDS)
    return router, proxy


def protected(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0)
    require(not info.st_mode & 0o022)
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0)
        require(not info.st_mode & 0o022)


def host_policy():
    protected(HOST_POLICY)
    return runpy.run_path(str(HOST_POLICY))


def ephemeral_range():
    low, high = (int(value) for value in PORT_RANGE.read_text().split())
    require(1 <= low <= high <= 65535)
    return low, high


def bindable(address, port):
    """Prove the address is local and the exact listener is free, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((address, port))


def check(document):
    require(os.geteuid() == 0)
    router, proxy = settings(document)
    host = host_policy()
    # Exact account, no host account inside either range, no overlap.
    host["identity"]()
    mapped = []
    for source, key in (
        ("/etc/subuid", "candidate_uid"),
        ("/etc/subgid", "candidate_gid"),
    ):
        start, end = host["mapping"](Path(source).read_text(), USER, [])
        value = start + document[key] - 1
        require(start <= value < end)
        mapped.append(value)
    low, high = ephemeral_range()
    for address, port in (router, proxy):
        require(port < low or port > high)
        bindable(address, port)
    return {
        "schema": "dittobench-coding-hosted-host-prerequisites-check-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "router_listen": document["router_listen"],
        "egress_proxy": document["egress_proxy"],
        "egress_network": document["egress_network"],
        "candidate_host_uid": mapped[0],
        "candidate_host_gid": mapped[1],
        "services_started": False,
        "private_execution_ready": False,
    }


def unique(entries):
    result = {}
    for key, value in entries:
        require(key not in result)
        result[key] = value
    return result


def main(argv, stream):
    require(argv == ["check"])
    body = stream.read(4097)
    require(0 < len(body) <= 4096)
    document = json.loads(body, object_pairs_hook=unique)
    print(json.dumps(check(document), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main(sys.argv[1:], sys.stdin.buffer)
    except (ValueError, KeyError, TypeError, OSError):
        print("coding hosted host prerequisites check failed", file=sys.stderr)
        raise SystemExit(1) from None
