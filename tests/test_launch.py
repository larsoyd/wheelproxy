import os
import socket
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCH = os.path.join(ROOT, "contrib", "wheelproxy-launch.sh")


class Launch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wl")
        self.out = os.path.join(self.tmp, "out")
        self.browser = os.path.join(self.tmp, "browser")
        with open(self.browser, "w") as f:
            f.write('#!/usr/bin/env bash\nprintf "%s\\n" "display=$WAYLAND_DISPLAY" "$@" > "' + self.out + '"\n')
        os.chmod(self.browser, 0o755)
        self.sock = os.path.join(self.tmp, "wayland-smooth")

    def launch(self, *args, code=0):
        env = dict(os.environ, XDG_RUNTIME_DIR=self.tmp, WAYLAND_DISPLAY="wayland-1")
        res = subprocess.run([LAUNCH] + list(args), env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(code, res.returncode, res.stderr)
        return res.stderr

    def lines(self):
        with open(self.out) as f:
            return f.read().splitlines()

    def test_live_proxy_is_used_and_smooth_scrolling_disabled(self):
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.sock)
        srv.listen(1)
        err = self.launch(self.browser, "--incognito", "https://example.com")
        srv.close()
        self.assertEqual(["display=wayland-smooth", "--disable-smooth-scrolling", "--incognito",
                          "https://example.com"], self.lines())
        self.assertIn("[launcher] via=proxy", err)

    def test_missing_proxy_falls_back_to_direct(self):
        err = self.launch(self.browser, "--incognito")
        self.assertEqual(["display=wayland-1", "--incognito"], self.lines())
        self.assertIn("[launcher] via=direct reason=no_proxy", err)

    def test_stale_socket_falls_back_to_direct(self):
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.sock)
        srv.close()
        err = self.launch(self.browser)
        self.assertEqual(["display=wayland-1"], self.lines())
        self.assertIn("[launcher] via=direct reason=no_proxy", err)

    def test_missing_browser_is_refused(self):
        err = self.launch(code=2)
        self.assertIn("[launcher] refused reason=no_browser", err)


if __name__ == "__main__":
    unittest.main()
