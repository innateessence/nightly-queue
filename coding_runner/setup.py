from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.sdist import sdist


class StandaloneSdist(sdist):
    def make_release_tree(self, base_dir, files):
        local_files = [name for name in files if ".." not in Path(name).parts]
        super().make_release_tree(base_dir, local_files)
        root = Path(__file__).resolve().parent
        companion = "vibearr_runner"
        source = root / "compat" / companion
        if not source.exists():
            source = root.parent / companion
        target = Path(base_dir) / "compat" / companion
        target.mkdir(parents=True, exist_ok=True)
        for path in source.rglob("*.py"):
            if path.name == "setup.py" or "build" in path.parts:
                continue
            relative = path.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        configuration = Path(base_dir) / "pyproject.toml"
        content = configuration.read_text().replace(
            f'"../{companion}"', f'"compat/{companion}"'
        )
        configuration.unlink()
        configuration.write_text(content)


setup(cmdclass={"sdist": StandaloneSdist})
