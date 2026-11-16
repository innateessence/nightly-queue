"""Small SCRAM-authenticated wire probe, independent of application database libraries."""

import base64
import hashlib
import hmac
import secrets
import socket
import struct
import time
import urllib.parse


def exact(stream, count):
    output = bytearray()
    while len(output) < count:
        data = stream.recv(count - len(output))
        if not data:
            raise RuntimeError("Disposable database disconnected.")
        output.extend(data)
    return bytes(output)


def receive(stream):
    kind = exact(stream, 1)
    length = struct.unpack("!I", exact(stream, 4))[0]
    if not 4 <= length <= 65536:
        raise RuntimeError("Invalid PostgreSQL response length.")
    return kind, exact(stream, length - 4)


def send(stream, kind, payload):
    stream.sendall(kind + struct.pack("!I", len(payload) + 4) + payload)


def query(url):
    parsed = urllib.parse.urlsplit(url)
    password = parsed.password or ""
    deadline = time.monotonic() + 30
    while True:
        try:
            stream = socket.create_connection((parsed.hostname, parsed.port), timeout=2)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    with stream:
        stream.settimeout(10)
        startup = (
            struct.pack("!I", 196608) + b"user\x00task\x00database\x00task\x00\x00"
        )
        stream.sendall(struct.pack("!I", len(startup) + 4) + startup)
        nonce = secrets.token_hex(16)
        first = "n=task,r=" + nonce
        signature = b""
        while True:
            kind, payload = receive(stream)
            if kind == b"E":
                raise RuntimeError("Disposable PostgreSQL authentication failed.")
            if kind == b"Z":
                break
            if kind != b"R":
                continue
            method = struct.unpack("!I", payload[:4])[0]
            if method == 10:
                initial = ("n,," + first).encode()
                send(
                    stream,
                    b"p",
                    b"SCRAM-SHA-256\x00" + struct.pack("!I", len(initial)) + initial,
                )
            elif method == 11:
                server_first = payload[4:].decode()
                fields = dict(part.split("=", 1) for part in server_first.split(","))
                if (
                    not fields["r"].startswith(nonce)
                    or not 1 <= int(fields["i"]) <= 1000000
                ):
                    raise RuntimeError("Invalid disposable database SCRAM parameters.")
                salted = hashlib.pbkdf2_hmac(
                    "sha256",
                    password.encode(),
                    base64.b64decode(fields["s"]),
                    int(fields["i"]),
                )
                client_key = hmac.digest(salted, b"Client Key", "sha256")
                final = "c=biws,r=" + fields["r"]
                auth = (first + "," + server_first + "," + final).encode()
                client_signature = hmac.digest(
                    hashlib.sha256(client_key).digest(), auth, "sha256"
                )
                proof = bytes(
                    left ^ right
                    for left, right in zip(client_key, client_signature, strict=True)
                )
                signature = hmac.digest(
                    hmac.digest(salted, b"Server Key", "sha256"), auth, "sha256"
                )
                send(
                    stream,
                    b"p",
                    (final + ",p=" + base64.b64encode(proof).decode()).encode(),
                )
            elif method == 12:
                expected = "v=" + base64.b64encode(signature).decode()
                if not hmac.compare_digest(payload[4:].decode(), expected):
                    raise RuntimeError(
                        "Disposable database SCRAM server signature does not match."
                    )
            elif method != 0:
                raise RuntimeError(
                    "Unexpected disposable database authentication protocol."
                )
        send(
            stream,
            b"Q",
            b"CREATE TABLE isolation(value integer); INSERT INTO isolation VALUES (42); SELECT value FROM isolation;\x00",
        )
        rows = []
        while True:
            kind, payload = receive(stream)
            if kind == b"E":
                raise RuntimeError("Disposable database was not fresh or query failed.")
            if kind == b"D":
                rows.append(payload)
            if kind == b"Z":
                break
        if rows != [b"\x00\x01\x00\x00\x00\x0242"]:
            raise RuntimeError("Disposable database returned unexpected data.")
    return parsed.hostname
