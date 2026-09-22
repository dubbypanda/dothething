"""Venv checks and repairs from dtt.sh, run against real venvs."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MINOR = "%d.%d" % sys.version_info[:2]


def shell_function(source, name):
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", source, re.M | re.S).group(0)


def venv_config(venv):
    lines = (venv / "pyvenv.cfg").read_text().splitlines()
    return dict(line.split(" = ", 1) for line in lines if " = " in line)


class EnsureVenvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "dtt.sh").read_text()
        cls.functions = shell_function(source, "venv_works") + shell_function(source, "ensure_venv")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name)
        self.venv = self.cache / "venv"
        self.marker = self.cache / ".deps"
        self.marker.touch()

    def ensure(self, environment=None):
        script = f'set -euo pipefail\n{self.functions}\nensure_venv "$1" "$2"\n'
        env = {**os.environ, "BASE_PYTHON": sys.executable, **(environment or {})}
        result = subprocess.run(
            ["bash", "-c", script, "bash", str(self.venv), str(self.marker)],
            env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def build(self, venv):
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)

    def add_package(self):
        (self.venv / "lib" / f"python{MINOR}" / "site-packages" / "dtt_probe.py").touch()

    def runs(self, code=""):
        try:
            result = subprocess.run([self.venv / "bin" / "python", "-c", code], capture_output=True)
        except OSError:
            return False
        return result.returncode == 0

    def imports_package(self):
        return self.runs("import dtt_probe")

    def delete_base_python(self):
        """Point the venv at an interpreter that no longer exists, as Homebrew
        leaves it after a patch upgrade deletes the old Cellar folder."""
        missing = self.cache / "Cellar" / "python" / "bin"
        links = [path for path in (self.venv / "bin").iterdir()
                 if path.is_symlink() and os.path.isabs(os.readlink(path))]
        self.assertTrue(links, "venv should link to its base interpreter")
        for path in links:
            path.unlink()
            path.symlink_to(missing / path.name)
        config = (self.venv / "pyvenv.cfg").read_text()
        config = re.sub(r"(?m)^home = .*$", f"home = {missing}", config)
        (self.venv / "pyvenv.cfg").write_text(config)
        self.assertFalse(self.runs())

    def test_creates_missing_venv_and_drops_its_markers(self):
        output = self.ensure()

        self.assertIn("Creating", output)
        self.assertTrue(self.runs())
        self.assertTrue((self.venv / "bin" / "pip").exists())
        self.assertFalse(self.marker.exists())

    def test_leaves_working_venv_alone(self):
        self.build(self.venv)
        self.add_package()

        self.assertEqual(self.ensure(), "")
        self.assertTrue(self.imports_package())
        self.assertTrue(self.marker.exists())

    def test_relinks_venv_whose_python_was_deleted_and_keeps_its_packages(self):
        self.build(self.venv)
        self.add_package()
        self.delete_base_python()

        output = self.ensure()

        self.assertIn("Relinking", output)
        self.assertNotIn("Rebuilding", output)
        self.assertTrue(self.imports_package())
        self.assertTrue(self.marker.exists())
        reference = self.cache / "reference"
        self.build(reference)
        self.assertEqual(venv_config(self.venv)["home"], venv_config(reference)["home"])

    def test_rebuilds_broken_venv_from_another_minor_version(self):
        self.build(self.venv)
        self.add_package()
        self.delete_base_python()
        config = (self.venv / "pyvenv.cfg").read_text()
        (self.venv / "pyvenv.cfg").write_text(
            re.sub(r"(?m)^version = .*$", "version = 2.7.18", config))

        output = self.ensure()

        self.assertIn("Rebuilding", output)
        self.assertNotIn("Relinking", output)
        self.assertTrue(self.runs())
        self.assertFalse(self.imports_package())
        self.assertFalse(self.marker.exists())

    def test_rebuilds_venv_whose_python_cannot_see_its_packages(self):
        self.build(self.venv)
        self.add_package()
        (self.venv / "lib" / f"python{MINOR}").rename(self.venv / "lib" / "python2.7")
        self.assertTrue(self.runs())

        output = self.ensure()

        self.assertIn("Rebuilding", output)
        self.assertNotIn("Relinking", output)
        self.assertTrue((self.venv / "lib" / f"python{MINOR}" / "site-packages").is_dir())
        self.assertFalse(self.marker.exists())

    def test_rebuilds_venv_that_relinking_does_not_fix(self):
        self.build(self.venv)
        self.add_package()
        self.delete_base_python()
        (self.venv / "bin" / "activate").unlink()

        output = self.ensure()

        self.assertIn("Relinking", output)
        self.assertIn("Rebuilding", output)
        self.assertTrue((self.venv / "bin" / "activate").exists())
        self.assertFalse(self.imports_package())
        self.assertFalse(self.marker.exists())

    def test_builds_from_base_python_while_another_venv_is_active(self):
        outer = self.cache / "outer"
        self.build(outer)

        self.ensure({"VIRTUAL_ENV": str(outer),
                     "PATH": f"{outer / 'bin'}{os.pathsep}{os.environ['PATH']}"})

        config = venv_config(self.venv)
        self.assertNotIn(str(outer), config["command"])
        self.assertEqual(config["home"], venv_config(outer)["home"])


if __name__ == "__main__":
    unittest.main()
