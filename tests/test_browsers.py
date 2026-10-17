import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "contrib", "wheelproxy-browsers.sh")
HOOK = os.path.join(ROOT, "contrib", "wheelproxy-browsers.hook")
LAUNCH = "/usr/bin/wheelproxy-launch"
MARKER = b"--disable-smooth-scrolling"

PACMAN = """#!/usr/bin/env bash
echo "pacman $*" >> "$STUB/calls"
case "$1" in
    -Qqo) awk -v f="$2" '$1 == f { print $2; found = 1 } END { exit !found }' "$STUB/owners" ;;
    -Qlq) cat "$STUB/files/$2" ;;
    *) exit 9 ;;
esac
"""

CHROME = """[Desktop Entry]
Name=Google Chrome
Exec=/usr/bin/google-chrome-stable %U
Categories=Network;WebBrowser;

[Desktop Action new-window]
Name=New Window
Exec=/usr/bin/google-chrome-stable
"""

CHROME_PORTAL = CHROME.replace("Categories=", "NoDisplay=true\nCategories=")

CHROMIUM = """[Desktop Entry]
Name=Chromium
Exec=chromium %U
MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;
"""

FIREFOX = """[Desktop Entry]
Name=Firefox
Exec=/usr/lib/firefox/firefox %u
Categories=Network;WebBrowser;
"""

CODE = """[Desktop Entry]
Name=Code
Exec=/usr/bin/code %F
Categories=Development;

[Desktop Action open-url]
Name=Open Link
MimeType=x-scheme-handler/https;
Categories=WebBrowser;
"""


def routed(text):
    return text.replace("Exec=", "Exec=" + LAUNCH + " ")


class Browsers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb")
        self.apps = os.path.join(self.tmp, "applications")
        self.stub = os.path.join(self.tmp, "stub")
        self.config = os.path.join(self.tmp, "browsers")
        bindir = os.path.join(self.tmp, "bin")
        for d in (self.apps, bindir, os.path.join(self.stub, "files")):
            os.makedirs(d)
        with open(os.path.join(bindir, "pacman"), "w") as f:
            f.write(PACMAN)
        os.chmod(os.path.join(bindir, "pacman"), 0o755)
        open(os.path.join(self.stub, "owners"), "w").close()
        self.env = dict(os.environ, STUB=self.stub, WHEELPROXY_APPS=self.apps, WHEELPROXY_CONFIG=self.config,
                        WHEELPROXY_LAUNCH=LAUNCH, PATH=bindir + os.pathsep + os.environ["PATH"])

    def binary(self, name, body):
        path = os.path.join(self.tmp, "pkgfiles", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path

    def package(self, pkg, entries, body):
        files = [self.binary(pkg + "/main", body)]
        for name, text in entries.items():
            path = self.entry(name, text)
            files.append(path)
            with open(os.path.join(self.stub, "owners"), "a") as f:
                f.write("%s %s\n" % (path, pkg))
        with open(os.path.join(self.stub, "files", pkg), "w") as f:
            f.write("\n".join([os.path.dirname(files[0]) + "/"] + files) + "\n")

    def entry(self, name, text):
        path = os.path.join(self.apps, name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o644)
        return path

    def read(self, name):
        with open(os.path.join(self.apps, name)) as f:
            return f.read()

    def configure(self, *keys):
        with open(self.config, "w") as f:
            f.write("# browsers routed through wheelproxy\n" + "".join(k + "\n" for k in keys))

    def standard(self):
        self.package("google-chrome", {"google-chrome.desktop": CHROME, "com.google.Chrome.desktop": CHROME_PORTAL},
                     b"\x7fELF\x02\x01" + MARKER + b"\x00tail")
        self.package("chromium", {"chromium.desktop": CHROMIUM}, b"\x7fELF\x02\x01" + b"x" * 70000 + MARKER)
        self.package("firefox", {"firefox.desktop": FIREFOX}, b"\x7fELF\x02\x01no marker here")
        self.package("code", {"code.desktop": CODE}, b"\x7fELF\x02\x01" + MARKER)

    def run_tool(self, *args, code=0, stdin=""):
        res = subprocess.run([TOOL, *args], env=self.env, input=stdin, capture_output=True, text=True,
                             timeout=20, umask=0o077)
        self.assertEqual(code, res.returncode, "stderr=" + res.stderr)
        return res

    def test_list_names_each_chromium_package_once_in_order(self):
        self.standard()
        for pkg in ("vivaldi", "brave", "zen", "opera"):
            self.package(pkg, {pkg + ".desktop": CHROMIUM.replace("Chromium", pkg.title())},
                         b"\x7fELF\x02\x01" + MARKER)
        out = self.run_tool("list").stdout
        self.assertEqual("brave\tBrave\nchromium\tChromium\ngoogle-chrome\tGoogle Chrome\nopera\tOpera\n"
                         "vivaldi\tVivaldi\nzen\tZen\n", out)

    def test_marker_outside_an_elf_file_is_not_chromium(self):
        self.package("lookalike", {"lookalike.desktop": CHROMIUM.replace("Chromium", "Lookalike")},
                     b"#!/bin/sh\nexec real " + MARKER + b"\n")
        res = self.run_tool("list")
        self.assertEqual("", res.stdout, "stderr=" + res.stderr)

    def test_unowned_entry_is_skipped_not_guessed(self):
        self.entry("handmade.desktop", CHROMIUM)
        self.configure("all")
        err = self.run_tool("apply").stderr
        self.assertIn("[browsers] skipped entry=handmade.desktop reason=unowned", err)
        self.assertEqual(CHROMIUM, self.read("handmade.desktop"))

    def test_apply_routes_selected_browser_entries_only(self):
        self.standard()
        self.configure("google-chrome")
        err = self.run_tool("apply").stderr
        self.assertEqual(routed(CHROME), self.read("google-chrome.desktop"))
        self.assertEqual(routed(CHROME_PORTAL), self.read("com.google.Chrome.desktop"))
        self.assertEqual(CHROMIUM, self.read("chromium.desktop"))
        self.assertEqual(FIREFOX, self.read("firefox.desktop"))
        self.assertEqual(CODE, self.read("code.desktop"))
        self.assertIn("[browsers] routed entry=google-chrome.desktop browser=google-chrome", err)
        self.assertIn("[browsers] skipped entry=chromium.desktop browser=chromium reason=not_selected", err)
        self.assertIn("[browsers] skipped entry=firefox.desktop browser=firefox reason=not_chromium", err)
        self.assertNotIn("code.desktop", err)
        self.assertEqual(0o644, os.stat(os.path.join(self.apps, "google-chrome.desktop")).st_mode & 0o777)

    def test_all_routes_every_chromium_browser(self):
        self.standard()
        self.configure("all")
        self.run_tool("apply")
        self.assertEqual(routed(CHROME), self.read("google-chrome.desktop"))
        self.assertEqual(routed(CHROMIUM), self.read("chromium.desktop"))
        self.assertEqual(FIREFOX, self.read("firefox.desktop"))
        self.assertEqual(CODE, self.read("code.desktop"))

    def test_apply_twice_never_wraps_twice(self):
        self.standard()
        self.configure("all")
        self.run_tool("apply")
        path = os.path.join(self.apps, "chromium.desktop")
        before = os.stat(path).st_mtime_ns
        err = self.run_tool("apply").stderr
        self.assertEqual(routed(CHROMIUM), self.read("chromium.desktop"))
        self.assertEqual(before, os.stat(path).st_mtime_ns)
        self.assertIn("[browsers] up_to_date entry=chromium.desktop browser=chromium", err)

    def test_hand_wrapped_line_is_kept_and_the_rest_wrapped(self):
        self.standard()
        self.configure("google-chrome")
        hand = "Exec=/opt/w/wheelproxy-launch /usr/bin/google-chrome-stable\n"
        partial = CHROME.replace("Exec=/usr/bin/google-chrome-stable\n", hand)
        self.entry("google-chrome.desktop", partial)
        self.run_tool("apply")
        want = partial.replace("Exec=/usr/bin", "Exec=" + LAUNCH + " /usr/bin")
        self.assertEqual(want, self.read("google-chrome.desktop"))

    def test_deselected_browser_is_restored(self):
        self.standard()
        self.configure("all")
        self.run_tool("apply")
        self.configure("chromium  # kept")
        err = self.run_tool("apply").stderr
        self.assertEqual(CHROME, self.read("google-chrome.desktop"))
        self.assertEqual(routed(CHROMIUM), self.read("chromium.desktop"))
        self.assertIn("[browsers] restored entry=google-chrome.desktop browser=google-chrome reason=not_selected", err)

    def test_hook_routes_only_the_targets_it_is_given(self):
        self.standard()
        self.configure("all")
        stdin = "usr/share/applications/chromium.desktop\nusr/share/applications/code.desktop\n"
        err = self.run_tool("hook", stdin=stdin).stderr
        self.assertEqual(routed(CHROMIUM), self.read("chromium.desktop"))
        self.assertEqual(CHROME, self.read("google-chrome.desktop"))
        self.assertEqual(CODE, self.read("code.desktop"))
        self.assertIn("[browsers] routed entry=chromium.desktop browser=chromium", err)

    def test_hook_rewraps_an_upgraded_entry(self):
        self.standard()
        self.configure("chromium")
        self.run_tool("apply")
        self.entry("chromium.desktop", CHROMIUM.replace("Name=", "Version=1.5\nName="))
        self.run_tool("hook", stdin="usr/share/applications/chromium.desktop\n")
        self.assertEqual(routed(CHROMIUM.replace("Name=", "Version=1.5\nName=")), self.read("chromium.desktop"))

    def test_hook_ignores_removed_targets(self):
        self.standard()
        self.configure("all")
        res = self.run_tool("hook", stdin="usr/share/applications/gone.desktop\n")
        self.assertEqual("", res.stderr)

    def test_chromium_scan_runs_once_per_package(self):
        self.standard()
        self.configure("all")
        self.run_tool("apply")
        with open(os.path.join(self.stub, "calls")) as f:
            calls = f.read().splitlines()
        self.assertEqual(1, calls.count("pacman -Qlq google-chrome"), "calls=%s" % calls)

    def test_revert_restores_every_routed_entry(self):
        self.standard()
        self.configure("all")
        self.run_tool("apply")
        err = self.run_tool("revert").stderr
        for name, text in (("google-chrome.desktop", CHROME), ("com.google.Chrome.desktop", CHROME_PORTAL),
                           ("chromium.desktop", CHROMIUM), ("firefox.desktop", FIREFOX)):
            self.assertEqual(text, self.read(name), name)
        self.assertIn("[browsers] restored entry=chromium.desktop reason=revert", err)
        self.assertNotIn("firefox", err)

    def test_missing_config_is_refused_and_nothing_changes(self):
        self.standard()
        err = self.run_tool("apply", code=1).stderr
        self.assertIn("[browsers] refused reason=no_config path=" + self.config, err)
        self.assertEqual(CHROMIUM, self.read("chromium.desktop"))

    def test_failed_package_listing_is_refused(self):
        self.standard()
        self.configure("all")
        os.remove(os.path.join(self.stub, "files", "chromium"))
        err = self.run_tool("apply", code=1).stderr
        self.assertIn("[browsers] refused entry=chromium.desktop browser=chromium reason=package_files", err)
        self.assertEqual(CHROMIUM, self.read("chromium.desktop"))
        self.assertEqual(routed(CHROME), self.read("google-chrome.desktop"))

    def test_unknown_command_is_refused(self):
        err = self.run_tool("frobnicate", code=2).stderr
        self.assertIn("[browsers] refused reason=unknown_command command=frobnicate", err)

    def test_hook_file_runs_the_tool_on_installed_entries(self):
        with open(HOOK) as f:
            text = f.read()
        for line in ("Type = Path", "Operation = Install", "Operation = Upgrade",
                     "Target = usr/share/applications/*.desktop", "When = PostTransaction",
                     "Exec = /usr/bin/wheelproxy-browsers hook", "NeedsTargets"):
            self.assertIn(line + "\n", text)


if __name__ == "__main__":
    unittest.main()
