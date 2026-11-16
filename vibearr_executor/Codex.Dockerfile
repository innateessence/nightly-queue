FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
LABEL io.vibearr.executor.protocol="1" io.vibearr.codex.version="0.169.0" io.vibearr.codex.review-purpose="1"
RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.39.5-0+deb12u3 iptables=1.8.9-2 ca-certificates=20230311+deb12u1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /input /state /work /secrets /home/task
RUN python - <<'PY'
import hashlib, io, platform, tarfile, urllib.request
archives = {
    "x86_64": ("x86_64", "9af4d751fce183faedea629437d5252dbf458eeb04130cf4f97be844621ea985"),
    "aarch64": ("aarch64", "97b97d87eea075c5cb7709c7f6156a451622a7667953ecda40e0276637196f3f"),
}
architecture, expected = archives[platform.machine()]
name = f"codex-{architecture}-unknown-linux-musl"
url = f"https://github.com/openai/codex/releases/download/rust-v0.169.0/{name}.tar.gz"
content = urllib.request.urlopen(url, timeout=120).read()
assert hashlib.sha256(content).hexdigest() == expected, "Codex release digest changed"
with tarfile.open(fileobj=io.BytesIO(content)) as archive:
    binary = archive.extractfile(name)
    assert binary is not None
    from pathlib import Path
    path = Path("/usr/local/bin/codex")
    path.write_bytes(binary.read())
    path.chmod(0o755)
PY
RUN test "$(codex --version)" = "codex-cli 0.169.0"
RUN python - <<'PY'
import hashlib, io, platform, tarfile, urllib.request
archives = {
    "x86_64": ("x86_64", "cbed6a8d25e8268792b757767fbc76d4c62e84bcf8ee4a63f97547f0de3aaceb"),
    "aarch64": ("aarch64", "059397e4d5beda7a7acc7441a6a56457d0f79e1001d129976ac0d26b0e6ee04c"),
}
architecture, expected = archives[platform.machine()]
name = f"codex-code-mode-host-{architecture}-unknown-linux-musl"
url = f"https://github.com/openai/codex/releases/download/rust-v0.169.0/{name}.tar.gz"
content = urllib.request.urlopen(url, timeout=120).read()
assert hashlib.sha256(content).hexdigest() == expected, "Codex code-mode host digest changed"
with tarfile.open(fileobj=io.BytesIO(content)) as archive:
    binary = archive.extractfile(name)
    assert binary is not None
    from pathlib import Path
    path = Path("/usr/local/bin/codex-code-mode-host")
    path.write_bytes(binary.read())
    path.chmod(0o755)
PY
RUN codex-code-mode-host --help >/dev/null
COPY vibearr_executor /opt/executor/vibearr_executor
RUN ln -s /opt/executor/vibearr_executor /usr/local/lib/python3.12/site-packages/vibearr_executor
ENV PYTHONPATH=/opt/executor PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/executor
ENTRYPOINT ["python", "-I", "-m", "vibearr_executor.worker"]
