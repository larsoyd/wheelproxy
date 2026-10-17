import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "contrib", "refresh-desktop-entry.sh")

SOURCE = """[Desktop Entry]
Version=1.0
Name=Google Chrome
Exec=/usr/bin/google-chrome-stable %U
MimeType=text/html;

[Desktop Action new-window]
Exec=/usr/bin/google-chrome-stable
"""


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wr")
        self.src = os.path.join(self.tmp, "system.desktop")
        self.dst = os.path.join(self.tmp, "apps", "google-chrome.desktop")
        self.notes = os.path.join(self.tmp, "notes")
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        with open(os.path.join(bindir, "kdialog"), "w") as f:
            f.write('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "' + self.notes + '"\n')
        os.chmod(os.path.join(bindir, "kdialog"), 0o755)
        self.env = dict(os.environ, WHEELPROXY_DESKTOP_SRC=self.src, WHEELPROXY_DESKTOP_DST=self.dst,
                        WHEELPROXY_LAUNCHER="/opt/x/google-chrome-smooth",
                        PATH=bindir + os.pathsep + os.environ["PATH"])
        self.write_source(SOURCE)

    def write_source(self, text):
        with open(self.src, "w") as f:
            f.write(text)

    def run_refresh(self, code=0):
        res = subprocess.run([SCRIPT], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(code, res.returncode, res.stderr)
        return res.stderr

    def dst_text(self):
        with open(self.dst) as f:
            return f.read()

    def notes_text(self):
        return open(self.notes).read() if os.path.exists(self.notes) else ""

    def test_missing_entry_is_created_with_launcher(self):
        err = self.run_refresh()
        self.assertEqual(SOURCE.replace("/usr/bin/google-chrome-stable", "/opt/x/google-chrome-smooth"),
                         self.dst_text())
        self.assertIn("[desktop] refreshed reason=missing", err)
        self.assertIn("--passivepopup", self.notes_text())

    def test_current_entry_is_left_alone(self):
        self.run_refresh()
        before = os.stat(self.dst).st_mtime_ns
        os.remove(self.notes)
        err = self.run_refresh()
        self.assertEqual(before, os.stat(self.dst).st_mtime_ns)
        self.assertIn("[desktop] up_to_date", err)
        self.assertEqual("", self.notes_text())

    def test_package_update_refreshes_entry(self):
        self.run_refresh()
        self.write_source(SOURCE.replace("Version=1.0", "Version=1.5"))
        err = self.run_refresh()
        self.assertIn("Version=1.5", self.dst_text())
        self.assertNotIn("/usr/bin/google-chrome-stable", self.dst_text())
        self.assertIn("[desktop] refreshed reason=changed", err)

    def test_missing_source_is_refused_and_entry_kept(self):
        self.run_refresh()
        os.remove(self.src)
        err = self.run_refresh(code=1)
        self.assertIn("[desktop] refused reason=no_source", err)
        self.assertIn("/opt/x/google-chrome-smooth", self.dst_text())

    def test_unknown_exec_is_refused_and_entry_kept(self):
        self.run_refresh()
        self.write_source(SOURCE.replace("/usr/bin/google-chrome-stable", "/usr/bin/chrome-new"))
        err = self.run_refresh(code=1)
        self.assertIn("[desktop] refused reason=no_exec_to_replace", err)
        self.assertIn("Version=1.0", self.dst_text())


if __name__ == "__main__":
    unittest.main()
