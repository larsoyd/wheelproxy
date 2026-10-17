import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "packaging", "wheelproxy-git")
PKG_ALL = os.path.join(ROOT, "packaging", "wheelproxy-all-git")

STUB = """#!/usr/bin/env bash
echo "${0##*/} $*" >> "$STUB/calls"
"""

PACMAN = """#!/usr/bin/env bash
case "$1" in
    -Qqo) echo chromium ;;
    -Qlq) echo "$STUB/chromium-bin" ;;
esac
"""


class Packaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp")
        self.stub = os.path.join(self.tmp, "stub")
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.stub)
        os.makedirs(bindir)
        for name, body in (("wheelproxy-browsers", STUB), ("systemctl", STUB), ("pacman", PACMAN)):
            with open(os.path.join(bindir, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(bindir, name), 0o755)
        apps = os.path.join(self.tmp, "apps")
        os.makedirs(apps)
        with open(os.path.join(apps, "chromium.desktop"), "w") as f:
            f.write("[Desktop Entry]\nName=Chromium\nExec=chromium\nCategories=WebBrowser;\n")
        with open(os.path.join(self.stub, "chromium-bin"), "wb") as f:
            f.write(b"\x7fELF\x02\x01disable-smooth-scrolling")
        os.chmod(os.path.join(self.stub, "chromium-bin"), 0o755)
        self.env = dict(os.environ, STUB=self.stub, WHEELPROXY_APPS=apps,
                        PATH=bindir + os.pathsep + os.environ["PATH"])

    def bash(self, script, pkg=PKG, stdin="", code=0):
        res = subprocess.run(["bash", "-c", script], cwd=pkg, env=self.env, input=stdin, capture_output=True,
                             text=True, timeout=60, umask=0o077)
        self.assertEqual(code, res.returncode, "stderr=" + res.stderr)
        return res

    def calls(self):
        path = os.path.join(self.stub, "calls")
        return open(path).read().splitlines() if os.path.exists(path) else []

    def build(self, pkg=PKG, stdin=""):
        srcdir = os.path.join(self.tmp, "src")
        pkgdir = os.path.join(self.tmp, "pkg")
        os.makedirs(srcdir)
        os.symlink(ROOT, os.path.join(srcdir, "wheelproxy"))
        script = ('set -e\nsource PKGBUILD\nsrcdir=%s pkgdir=%s\ncd "$srcdir" && prepare\n'
                  'cd "$srcdir" && package\n') % (srcdir, pkgdir)
        res = self.bash(script, pkg=pkg, stdin=stdin)
        return pkgdir, res

    def test_install_hooks_apply_enable_and_revert(self):
        self.bash("source wheelproxy.install && post_install")
        self.assertEqual(["wheelproxy-browsers apply", "systemctl --global enable wheelproxy.service"],
                         self.calls())
        os.remove(os.path.join(self.stub, "calls"))
        self.bash("source wheelproxy.install && post_upgrade")
        self.assertEqual(["wheelproxy-browsers apply"], self.calls())
        os.remove(os.path.join(self.stub, "calls"))
        self.bash("source wheelproxy.install && pre_remove")
        self.assertEqual(["wheelproxy-browsers revert", "systemctl --global disable wheelproxy.service"],
                         self.calls())

    def test_srcinfo_keeps_config_and_runs_install_script(self):
        info = set(self.bash("makepkg --printsrcinfo").stdout.replace("\t", "").splitlines())
        for line in ("pkgname = wheelproxy-git", "backup = etc/wheelproxy/browsers", "install = wheelproxy.install",
                     "checkdepends = strace", "provides = wheelproxy", "conflicts = wheelproxy"):
            self.assertIn(line, info)

    def test_package_holds_the_chosen_browsers_and_system_paths(self):
        subprocess.run(["make", "-C", ROOT, "build/wheelproxy"], check=True, capture_output=True, timeout=120)
        pkgdir, res = self.build(stdin="1\n")
        self.assertIn("1) Chromium (chromium)", res.stderr)
        with open(os.path.join(pkgdir, "etc/wheelproxy/browsers")) as f:
            self.assertEqual("chromium\n", f.read())
        modes = {"usr/bin/wheelproxy": 0o755, "usr/bin/wheelproxy-launch": 0o755,
                 "usr/bin/wheelproxy-browsers": 0o755, "usr/share/libalpm/hooks/wheelproxy-browsers.hook": 0o644,
                 "usr/lib/systemd/user/wheelproxy.service": 0o644, "etc/wheelproxy/browsers": 0o644,
                 "usr/share/licenses/wheelproxy-git/LICENSE": 0o644}
        for path, mode in modes.items():
            full = os.path.join(pkgdir, path)
            self.assertTrue(os.path.isfile(full), path)
            self.assertEqual(mode, os.stat(full).st_mode & 0o777, path)
        with open(os.path.join(pkgdir, "usr/lib/systemd/user/wheelproxy.service")) as f:
            self.assertIn("ExecStart=/usr/bin/wheelproxy --listen wayland-smooth\n", f.read())

    def test_build_stops_when_no_browser_is_picked(self):
        srcdir = os.path.join(self.tmp, "src")
        os.makedirs(srcdir)
        os.symlink(ROOT, os.path.join(srcdir, "wheelproxy"))
        res = self.bash('set -e\nsource PKGBUILD\nsrcdir=%s\ncd "$srcdir" && prepare\necho reached' % srcdir,
                        code=2)
        self.assertNotIn("reached", res.stdout)
        self.assertIn("[browsers] refused reason=no_answer", res.stderr)

    def test_all_package_routes_every_browser_without_asking(self):
        subprocess.run(["make", "-C", ROOT, "build/wheelproxy"], check=True, capture_output=True, timeout=120)
        pkgdir, res = self.build(pkg=PKG_ALL)
        self.assertNotIn("1)", res.stderr)
        with open(os.path.join(pkgdir, "etc/wheelproxy/browsers")) as f:
            self.assertEqual("all\n", f.read())
        self.assertEqual(0o644, os.stat(os.path.join(pkgdir, "etc/wheelproxy/browsers")).st_mode & 0o777)
        for path in ("usr/bin/wheelproxy", "usr/bin/wheelproxy-launch", "usr/bin/wheelproxy-browsers",
                     "usr/share/libalpm/hooks/wheelproxy-browsers.hook", "usr/lib/systemd/user/wheelproxy.service",
                     "usr/share/licenses/wheelproxy-all-git/LICENSE"):
            self.assertTrue(os.path.isfile(os.path.join(pkgdir, path)), path)

    def test_all_package_srcinfo_conflicts_with_the_picking_one(self):
        info = set(self.bash("makepkg --printsrcinfo", pkg=PKG_ALL).stdout.replace("\t", "").splitlines())
        for line in ("pkgname = wheelproxy-all-git", "backup = etc/wheelproxy/browsers",
                     "install = wheelproxy.install", "provides = wheelproxy", "conflicts = wheelproxy"):
            self.assertIn(line, info)

    def test_both_packages_share_one_install_script(self):
        with open(os.path.join(PKG, "wheelproxy.install")) as a, open(os.path.join(PKG_ALL, "wheelproxy.install")) as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
