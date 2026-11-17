"""Verify and install immutable vendor CLI artifacts during image build."""

import base64
import hashlib
import io
from pathlib import Path
import platform
import tarfile
import urllib.request


RELEASES = {
    "x86_64": (
        "x64",
        "x86_64",
        "t6aNIvNa1T+ZR5IkJARqjTy+U5LH59FuWok4QoXa/RpT4C0njeeE/SdUzvvhwmH3ji/Rh6EX4zgbx/v5yGtG8Q==",
        "588310a336fa3b9df36b3f0225b26f679d453efaad0cf3d66c3651ef3ec1647a",
        "9af4d751fce183faedea629437d5252dbf458eeb04130cf4f97be844621ea985",
        "cbed6a8d25e8268792b757767fbc76d4c62e84bcf8ee4a63f97547f0de3aaceb",
    ),
    "aarch64": (
        "arm64",
        "aarch64",
        "XYVJwJulEKmlbaYZnYQiRaoZTdmBL+A94cRJO4J50txbHe9+T7TErgxFCLTd7AVZ8GX+PGulSBnKmiuzJhkcGg==",
        "fae5be262d693b7c5e562856cc473e59e741772936db0c0600ac0d5ed5bd088b",
        "97b97d87eea075c5cb7709c7f6156a451622a7667953ecda40e0276637196f3f",
        "059397e4d5beda7a7acc7441a6a56457d0f79e1001d129976ac0d26b0e6ee04c",
    ),
}


def install(url, member, binary, expected, *, algorithm="sha256", encoding="hex"):
    with urllib.request.urlopen(url, timeout=180) as response:
        content = response.read(300 * 1024 * 1024)
        if response.read(1):
            raise RuntimeError("CLI archive exceeds build limit.")
    actual = hashlib.new(algorithm, content).digest()
    observed = (
        base64.b64encode(actual).decode() if encoding == "base64" else actual.hex()
    )
    if observed != expected:
        raise RuntimeError("Vendor CLI archive checksum changed.")
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        stream = archive.extractfile(member)
        if stream is None:
            raise RuntimeError("Vendor CLI executable is absent.")
        path = Path("/usr/local/bin") / binary
        path.write_bytes(stream.read())
        path.chmod(0o755)


def main():
    arch, rust_arch, claude, opencode, codex, code_host = RELEASES[platform.machine()]
    package = f"claude-code-linux-{arch}"
    install(
        f"https://registry.npmjs.org/@anthropic-ai/{package}/-/{package}-2.1.118.tgz",
        "package/claude",
        "claude",
        claude,
        algorithm="sha512",
        encoding="base64",
    )
    install(
        f"https://github.com/anomalyco/opencode/releases/download/v1.19.5/opencode-linux-{arch}.tar.gz",
        "opencode",
        "opencode",
        opencode,
    )
    for binary, checksum in (("codex", codex), ("codex-code-mode-host", code_host)):
        member = f"{binary}-{rust_arch}-unknown-linux-musl"
        install(
            f"https://github.com/openai/codex/releases/download/rust-v0.169.0/{member}.tar.gz",
            member,
            binary,
            checksum,
        )


if __name__ == "__main__":
    main()
