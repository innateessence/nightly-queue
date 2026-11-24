"""Restrict task-UID traffic inside this worker's own network namespace."""

import ipaddress
import socket
import subprocess


CHAIN = "VIBEARR_TASK"


def address(host):
    values = {entry[4][0] for entry in socket.getaddrinfo(host, None, socket.AF_INET)}
    if len(values) != 1:
        raise RuntimeError("Worker endpoint must resolve to one private IPv4 address.")
    value = ipaddress.IPv4Address(values.pop())
    if not value.is_private or value.is_loopback or value.is_link_local:
        raise RuntimeError("Worker endpoint must be on its private Docker network.")
    return str(value)


def configure(destinations, *, blocked_ports="8791,9001", task_uids=(1000,)):
    if not task_uids or any(
        type(uid) is not int or uid not in {1000, 10003} for uid in task_uids
    ):
        raise ValueError("Register task and candidate-service UIDs explicitly.")
    for executable in ("iptables", "ip6tables"):

        def run(*arguments):
            subprocess.run(
                [executable, "-w", "5", *arguments], check=True, capture_output=True
            )

        exists = (
            subprocess.run(
                [executable, "-w", "5", "-S", CHAIN], capture_output=True
            ).returncode
            == 0
        )
        if not exists:
            run("-N", CHAIN)
            run("-A", CHAIN, "-j", "REJECT")
        for uid in sorted(set(task_uids)):
            owner = ["-m", "owner", "--uid-owner", str(uid), "-j", CHAIN]
            installed = (
                subprocess.run(
                    [executable, "-w", "5", "-C", "OUTPUT", *owner], capture_output=True
                ).returncode
                == 0
            )
            if not installed:
                run("-I", "OUTPUT", "1", *owner)
        # Keep the terminal rejection in place while replacing a job's allowlist.
        for line in subprocess.check_output(
            [executable, "-w", "5", "-S", CHAIN], text=True
        ).splitlines():
            if line.startswith("-A ") and "-j REJECT" not in line:
                run(*["-D", *line.split()[1:]])
        loopback = "127.0.0.1/32" if executable == "iptables" else "::1/128"
        run(
            "-I",
            CHAIN,
            "1",
            "-p",
            "tcp",
            "-d",
            loopback,
            "-m",
            "multiport",
            "!",
            "--dports",
            blocked_ports,
            "-j",
            "ACCEPT",
        )
        if executable == "iptables":
            for host, port in destinations:
                run(
                    "-I",
                    CHAIN,
                    "1",
                    "-p",
                    "tcp",
                    "-d",
                    host,
                    "--dport",
                    str(port),
                    "-j",
                    "ACCEPT",
                )
