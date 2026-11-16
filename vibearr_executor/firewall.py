"""One-use trusted network helper. The task never receives NET_ADMIN."""

import ipaddress
import subprocess
import sys


def main() -> None:
    destinations = []
    for value in sys.argv[1:]:
        address, port = value.split(":")
        destinations.append((str(ipaddress.IPv4Address(address)), str(int(port))))
    for executable in ("iptables", "ip6tables"):
        for chain in ("INPUT", "OUTPUT", "FORWARD"):
            subprocess.run([executable, "-P", chain, "DROP"], check=True)
        subprocess.run(
            [
                executable,
                "-A",
                "INPUT",
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            ],
            check=True,
        )
    subprocess.run(
        ["iptables", "-A", "OUTPUT", "-d", "127.0.0.1/32", "-o", "lo", "-j", "ACCEPT"],
        check=True,
    )
    subprocess.run(
        ["iptables", "-A", "INPUT", "-s", "127.0.0.1/32", "-i", "lo", "-j", "ACCEPT"],
        check=True,
    )
    for address, port in destinations:
        subprocess.run(
            [
                "iptables",
                "-A",
                "OUTPUT",
                "-d",
                address,
                "-p",
                "tcp",
                "--dport",
                port,
                "-j",
                "ACCEPT",
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
