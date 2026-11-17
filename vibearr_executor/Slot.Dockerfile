# syntax=docker/dockerfile:1
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS worker
LABEL io.vibearr.worker.protocol="2"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
RUN apt-get update && apt-get install -y --no-install-recommends git iptables ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /input /control /work /secrets /home/task /opt/validation /opt/candidate
COPY vibearr_executor/validation-requirements.txt /opt/validation/requirements.txt
COPY requirements.txt /opt/validation/project-requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/validation/requirements.txt -r /opt/validation/project-requirements.txt \
    && python -m venv --copies /opt/application-python \
    && /opt/application-python/bin/python -m pip install --no-cache-dir pip==25.0.1 setuptools==80.9.0 wheel==0.48.0 packaging==26.3
COPY vibearr_executor /opt/executor/vibearr_executor
RUN ln -s /opt/executor/vibearr_executor /usr/local/lib/python3.12/site-packages/vibearr_executor
WORKDIR /opt/executor
ENTRYPOINT ["python", "-I", "-m", "vibearr_executor.slot_server"]

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS native-binaries
RUN python - <<'PY'
import hashlib, io, platform, tarfile, urllib.request
from pathlib import Path
releases = {
    'x86_64': ('x86_64', '9af4d751fce183faedea629437d5252dbf458eeb04130cf4f97be844621ea985', 'cbed6a8d25e8268792b757767fbc76d4c62e84bcf8ee4a63f97547f0de3aaceb'),
    'aarch64': ('aarch64', '97b97d87eea075c5cb7709c7f6156a451622a7667953ecda40e0276637196f3f', '059397e4d5beda7a7acc7441a6a56457d0f79e1001d129976ac0d26b0e6ee04c'),
}
architecture, cli_digest, host_digest = releases[platform.machine()]
for tool, expected in [('codex', cli_digest), ('codex-code-mode-host', host_digest)]:
    name = f'{tool}-{architecture}-unknown-linux-musl'
    url = f'https://github.com/openai/codex/releases/download/rust-v0.169.0/{name}.tar.gz'
    content = urllib.request.urlopen(url, timeout=120).read()
    assert hashlib.sha256(content).hexdigest() == expected, 'Pinned native binary digest changed'
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        binary = archive.extractfile(name)
        assert binary is not None
        target = Path('/usr/local/bin') / tool
        target.write_bytes(binary.read())
        target.chmod(0o755)
PY
RUN test "$(codex --version)" = "codex-cli 0.169.0" && codex-code-mode-host --help >/dev/null

FROM worker AS agent
COPY --from=native-binaries /usr/local/bin/codex /usr/local/bin/codex
COPY --from=native-binaries /usr/local/bin/codex-code-mode-host /usr/local/bin/codex-code-mode-host
