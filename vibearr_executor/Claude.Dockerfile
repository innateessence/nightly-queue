FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
LABEL io.vibearr.executor.protocol="1" io.vibearr.claude.version="2.1.325"
RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.39.5-0+deb12u3 iptables=1.8.9-2 ca-certificates=20230311+deb12u1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /input /state /work /secrets /home/task
RUN python - <<'PY'
import base64, hashlib, io, platform, tarfile, urllib.request
from pathlib import Path
archives = {
    "x86_64": ("x64", "BqZzMvP/zgLLL34S+7OzbG+cyoFp7gwWHU5koVwsyMlTAz+0zZAmbY+UkzvtN2RXxp6AcMenbYIjQJxoXfUY3w=="),
    "aarch64": ("arm64", "NCvQcOxr685Y4mkYUjQT8l/Ouzw0k2xWloWeyzfsUtWiOedHosPkaW6XCSpzAl1dMuvDNBF97qpWgVDrSdK2Vw=="),
}
architecture, integrity = archives[platform.machine()]
name = f"claude-code-linux-{architecture}"
url = f"https://registry.npmjs.org/@anthropic-ai/{name}/-/{name}-2.1.325.tgz"
content = urllib.request.urlopen(url, timeout=120).read()
assert hashlib.sha512(content).digest() == base64.b64decode(integrity), "Claude release integrity changed"
with tarfile.open(fileobj=io.BytesIO(content)) as archive:
    entry = archive.getmember("package/claude")
    assert entry.isfile() and not entry.issym() and not entry.islnk()
    binary = archive.extractfile(entry)
    assert binary is not None
    path = Path("/usr/local/bin/claude")
    path.write_bytes(binary.read())
    path.chmod(0o755)
PY
RUN test "$(claude --version)" = "2.1.325 (Claude Code)"
COPY vibearr_executor /opt/executor/vibearr_executor
RUN ln -s /opt/executor/vibearr_executor /usr/local/lib/python3.12/site-packages/vibearr_executor
ENV PYTHONPATH=/opt/executor PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DISABLE_UPDATES=1
WORKDIR /opt/executor
ENTRYPOINT ["python", "-I", "-m", "vibearr_executor.worker"]
