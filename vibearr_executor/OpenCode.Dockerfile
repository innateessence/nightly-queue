FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
LABEL io.vibearr.executor.protocol="1" io.vibearr.opencode.version="1.19.5"
RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.39.5-0+deb12u3 iptables=1.8.9-2 ca-certificates=20230311+deb12u1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /input /state /work /secrets /home/task
RUN python - <<'PY'
import hashlib, io, platform, tarfile, urllib.request
from pathlib import Path
archives = {
    "x86_64": ("x64-baseline", "17c200fd4b2289954cc01552559f9b6d03ec157b2ddfb5eb54144f491656333d"),
    "aarch64": ("arm64", "fae5be262d693b7c5e562856cc473e59e741772936db0c0600ac0d5ed5bd088b"),
}
architecture, expected = archives[platform.machine()]
url = f"https://github.com/anomalyco/opencode/releases/download/v1.19.5/opencode-linux-{architecture}.tar.gz"
content = urllib.request.urlopen(url, timeout=120).read()
assert hashlib.sha256(content).hexdigest() == expected, "OpenCode release digest changed"
with tarfile.open(fileobj=io.BytesIO(content)) as archive:
    binary = archive.extractfile("opencode")
    assert binary is not None
    path = Path("/usr/local/bin/opencode")
    path.write_bytes(binary.read())
    path.chmod(0o755)
PY
RUN HOME=/tmp/opencode-build XDG_DATA_HOME=/tmp/opencode-build/data XDG_CONFIG_HOME=/tmp/opencode-build/config \
    XDG_CACHE_HOME=/tmp/opencode-build/cache XDG_STATE_HOME=/tmp/opencode-build/state \
    OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_MODELS_FETCH=1 \
    opencode --version | python -c 'import sys; assert sys.stdin.read().strip() == "1.19.5"'
RUN rm -rf /tmp/opencode-build
COPY vibearr_executor /opt/executor/vibearr_executor
RUN ln -s /opt/executor/vibearr_executor /usr/local/lib/python3.12/site-packages/vibearr_executor
ENV PYTHONPATH=/opt/executor PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/executor
ENTRYPOINT ["python", "-I", "-m", "vibearr_executor.worker"]
