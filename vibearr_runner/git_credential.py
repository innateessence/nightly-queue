"""Git credential helper: secret stays in a private supervisor descriptor."""

import os
import sys


def main():
    if len(sys.argv) != 5 or sys.argv[4] != "get":
        return
    fields = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if "=" in line)
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        return
    if fields.get("path", "").removesuffix(".git") != sys.argv[3]:
        return
    path = f"/proc/{int(sys.argv[1])}/fd/{int(sys.argv[2])}"
    with open(path, "rb") as stream:
        secret = stream.read(65537)
    if len(secret) > 65536 or any(
        character in secret for character in (b"\n", b"\r", b"\0")
    ):
        return
    os.write(1, b"username=x-access-token\npassword=" + secret + b"\n\n")


if __name__ == "__main__":
    main()
