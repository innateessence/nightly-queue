"""Image-owned, model-free Django fixture for current-boot browser qualification."""

import json
import os
from pathlib import Path
import subprocess

from .artifacts import snapshot
from .contracts import canonical, digest

ROOT = Path("/opt/browser-fixture")
FILES = {
    "vibearr_app/__init__.py": "",
    "vibearr_app/settings.py": """import os
from pathlib import Path
assert os.getuid() == 10003 and os.getgroups() == []
status = Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in status and 'NoNewPrivs:\\t1' in status
try:
    list(Path('/control').iterdir())
except PermissionError:
    pass
else:
    raise RuntimeError('Candidate app accessed trusted worker control.')
BASE_DIR = Path(__file__).resolve().parent.parent
try:
    (BASE_DIR / '.app-write-probe').write_text('unexpected')
except PermissionError:
    pass
else:
    raise RuntimeError('Candidate app source was writable.')
SECRET_KEY = 'qualification-only'
INSTALLED_APPS = ['django.contrib.staticfiles']
MIDDLEWARE = ['whitenoise.middleware.WhiteNoiseMiddleware']
ROOT_URLCONF = 'vibearr_app.urls'
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
DATABASES = {}
""",
    "vibearr_app/urls.py": """from django.http import HttpResponse
from django.urls import path
def index(request):
    theme = 'dark' if request.GET.get('theme') == 'dark' else 'light'
    rows = ''.join('<article><h2>Qualification task %s</h2><p>Worker-local source, disposable database, and isolated browser evidence.</p></article>' % n for n in range(14))
    return HttpResponse('<!doctype html><html data-theme="%s"><head><link rel="stylesheet" href="/static/work_queue/queue.css"></head><body><main><h1>Worker browser qualification</h1>%s</main></body></html>' % (theme, rows))
urlpatterns = [path('', index), path('health/', lambda request: HttpResponse('ok'))]
""",
    "static/work_queue/queue.css": """html{font-family:sans-serif;background:#f7f5f0;color:#17263b}html[data-theme=dark]{background:#132034;color:#f0f3f9}main{max-width:1100px;margin:40px auto;padding:20px}h1{font-size:38px}article{border:1px solid #70849c;border-radius:12px;margin:16px 0;padding:16px}h2{font-size:22px}p{line-height:1.5}@media(max-width:600px){main{margin:0;padding:12px}h1{font-size:28px}}
""",
}


def install():
    ROOT.mkdir(parents=True)
    for name, content in FILES.items():
        path = ROOT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Vibearr",
        "GIT_AUTHOR_EMAIL": "fixture@invalid",
        "GIT_COMMITTER_NAME": "Vibearr",
        "GIT_COMMITTER_EMAIL": "fixture@invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    for arguments in (
        ("init", "-q"),
        ("add", "."),
        ("commit", "-qm", "browser qualification fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=ROOT, env=environment, check=True)
    identities = {}
    for field, ref in (("revision", "HEAD"), ("tree_sha", "HEAD^{tree}")):
        identities[field] = (
            subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT)
            .decode()
            .strip()
        )
    identities["source_digest"] = digest(snapshot(ROOT))
    ROOT.with_suffix(".json").write_bytes(canonical(identities))


def source():
    content = snapshot(ROOT)
    identities = json.loads(ROOT.with_suffix(".json").read_bytes())
    if digest(content) != identities["source_digest"]:
        raise RuntimeError("The installed browser qualification fixture changed.")
    return content, identities


if __name__ == "__main__":
    install()
