import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "contrib", "wheelproxy-desktop.sh")
LAUNCH = "/opt/x/wheelproxy-launch"

CHROME = """[Desktop Entry]
Version=1.0
Name=Google Chrome
Exec=/usr/bin/google-chrome-stable %U
MimeType=text/html;

[Desktop Action new-window]
Exec=/usr/bin/google-chrome-stable
"""

BRAVE = """[Desktop Entry]
Name=Brave
Exec=brave %U
"""


def routed(text):
    return text.replace("Exec=", "Exec=" + LAUNCH + " ")


class Desktop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wd")
        self.src = os.path.join(self.tmp, "system")
        self.dst = os.path.join(self.tmp, "local")
        self.notes = os.path.join(self.tmp, "notes")
        os.mkdir(self.src)
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        with open(os.path.join(bindir, "kdialog"), "w") as f:
            f.write('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "' + self.notes + '"\n')
        os.chmod(os.path.join(bindir, "kdialog"), 0o755)
        self.env = dict(os.environ, WHEELPROXY_APPS_SRC=self.src, WHEELPROXY_APPS_DST=self.dst,
                        WHEELPROXY_LAUNCH=LAUNCH, PATH=bindir + os.pathsep + os.environ["PATH"])
        self.write("google-chrome.desktop", CHROME)

    def write(self, name, text):
        with open(os.path.join(self.src, name), "w") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.dst, name)) as f:
            return f.read()

    def run_tool(self, *names, code=0):
        res = subprocess.run([TOOL] + list(names), env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(code, res.returncode, res.stderr)
        return res.stderr

    def notes_text(self):
        return open(self.notes).read() if os.path.exists(self.notes) else ""

    def test_missing_entry_is_created_with_every_exec_routed(self):
        err = self.run_tool("google-chrome.desktop")
        self.assertEqual(routed(CHROME), self.read("google-chrome.desktop"))
        self.assertIn("[desktop] refreshed entry=google-chrome.desktop reason=missing", err)
        self.assertIn("--passivepopup", self.notes_text())

    def test_several_entries_in_one_run(self):
        self.write("brave-browser.desktop", BRAVE)
        self.run_tool("google-chrome.desktop", "brave-browser.desktop")
        self.assertEqual(routed(BRAVE), self.read("brave-browser.desktop"))
        self.assertEqual(routed(CHROME), self.read("google-chrome.desktop"))

    def test_current_entry_is_left_alone(self):
        self.run_tool("google-chrome.desktop")
        path = os.path.join(self.dst, "google-chrome.desktop")
        before = os.stat(path).st_mtime_ns
        os.remove(self.notes)
        err = self.run_tool("google-chrome.desktop")
        self.assertEqual(before, os.stat(path).st_mtime_ns)
        self.assertIn("[desktop] up_to_date entry=google-chrome.desktop", err)
        self.assertEqual("", self.notes_text())

    def test_package_update_refreshes_entry(self):
        self.run_tool("google-chrome.desktop")
        self.write("google-chrome.desktop", CHROME.replace("Version=1.0", "Version=1.5"))
        err = self.run_tool("google-chrome.desktop")
        self.assertEqual(routed(CHROME.replace("Version=1.0", "Version=1.5")), self.read("google-chrome.desktop"))
        self.assertIn("[desktop] refreshed entry=google-chrome.desktop reason=changed", err)

    def test_missing_source_is_refused_and_others_still_run(self):
        self.run_tool("google-chrome.desktop")
        os.remove(os.path.join(self.src, "google-chrome.desktop"))
        self.write("brave-browser.desktop", BRAVE)
        err = self.run_tool("google-chrome.desktop", "brave-browser.desktop", code=1)
        self.assertIn("[desktop] refused entry=google-chrome.desktop reason=no_source", err)
        self.assertEqual(routed(CHROME), self.read("google-chrome.desktop"))
        self.assertEqual(routed(BRAVE), self.read("brave-browser.desktop"))

    def test_entry_without_exec_is_refused(self):
        self.write("odd.desktop", "[Desktop Entry]\nName=Odd\n")
        err = self.run_tool("odd.desktop", code=1)
        self.assertIn("[desktop] refused entry=odd.desktop reason=no_exec", err)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "odd.desktop")))

    def test_no_entries_is_refused(self):
        err = self.run_tool(code=2)
        self.assertIn("[desktop] refused reason=no_entries", err)


if __name__ == "__main__":
    unittest.main()
