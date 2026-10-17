import os
import socket
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, "contrib", "google-chrome-smooth.sh")


class Launcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wl")
        self.out = os.path.join(self.tmp, "out")
        self.fake = os.path.join(self.tmp, "chrome")
        with open(self.fake, "w") as f:
            f.write('#!/usr/bin/env bash\nprintf "%s\\n" "display=$WAYLAND_DISPLAY" "$@" > "' + self.out + '"\n')
        os.chmod(self.fake, 0o755)
        self.sock = os.path.join(self.tmp, "wayland-smooth")

    def launch(self, *args):
        env = dict(os.environ, XDG_RUNTIME_DIR=self.tmp, WAYLAND_DISPLAY="wayland-1",
                   WHEELPROXY_CHROME=self.fake)
        res = subprocess.run([LAUNCHER] + list(args), env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, res.returncode, res.stderr)
        with open(self.out) as f:
            return f.read().splitlines(), res.stderr

    def test_live_proxy_is_used_and_smooth_scrolling_disabled(self):
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.sock)
        srv.listen(1)
        lines, err = self.launch("--incognito", "https://example.com")
        srv.close()
        self.assertEqual(["display=wayland-smooth", "--disable-smooth-scrolling", "--incognito",
                          "https://example.com"], lines)
        self.assertIn("[launcher] via=proxy", err)

    def test_missing_proxy_falls_back_to_direct(self):
        lines, err = self.launch("--incognito")
        self.assertEqual(["display=wayland-1", "--incognito"], lines)
        self.assertIn("[launcher] via=direct reason=no_proxy", err)

    def test_stale_socket_falls_back_to_direct(self):
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.sock)
        srv.close()
        lines, err = self.launch()
        self.assertEqual(["display=wayland-1"], lines)
        self.assertIn("[launcher] via=direct reason=no_proxy", err)


if __name__ == "__main__":
    unittest.main()
