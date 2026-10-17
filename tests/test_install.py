import os
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")

STUBS = {
    "make": """
echo "make $*" >> "$STUB/calls"
[ "${FAIL_MAKE:-}" = "$1" ] && exit 4
exit 0
""",
    "systemctl": """
echo "systemctl $*" >> "$STUB/calls"
case "$*" in
    *is-active*) s=$(cat "$STUB/state"); [[ "$*" == *--quiet* ]] || echo "$s"; [ "$s" = active ] ;;
    *show*) cat "$STUB/pid" ;;
    *restart*|*" start "*) [ -n "${NO_START:-}" ] || { echo active > "$STUB/state"; echo 200 > "$STUB/pid"; } ;;
esac
""",
    "ss": """
cat "$STUB/ss"
""",
    "journalctl": """
echo "journalctl $*" >> "$STUB/calls"
echo "[wheelproxy] start pid=$(cat "$STUB/pid") listen=x"
""",
}


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wi")
        self.stub = os.path.join(self.tmp, "stub")
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(self.stub)
        os.mkdir(bindir)
        for name, body in STUBS.items():
            path = os.path.join(bindir, name)
            with open(path, "w") as f:
                f.write("#!/usr/bin/env bash\n" + body)
            os.chmod(path, 0o755)
        self.sock = os.path.join(self.tmp, "wayland-smooth")
        self.env = dict(os.environ, STUB=self.stub, XDG_RUNTIME_DIR=self.tmp,
                        PATH=bindir + os.pathsep + os.environ["PATH"])
        self.service("inactive", 0)
        self.clients()

    def service(self, state, pid):
        self.put("state", state)
        self.put("pid", str(pid))

    def clients(self, n=0, others=0):
        lines = ["u_str LISTEN 0 16 %s 11 * 0" % self.sock]
        lines += ["u_str ESTAB 0 0 %s %d * %d" % (self.sock, 20 + i, 40 + i) for i in range(n)]
        lines += ["u_str ESTAB 0 0 /run/user/1000/wayland-0 %d * %d" % (60 + i, 80 + i) for i in range(others)]
        self.put("ss", "\n".join(lines) + "\n")

    def put(self, name, text):
        with open(os.path.join(self.stub, name), "w") as f:
            f.write(text + "\n")

    def run_install(self, *args, code=0, env=None, timeout=20):
        res = subprocess.run([INSTALL, *args], env=dict(self.env, **(env or {})), capture_output=True, text=True,
                             timeout=timeout)
        self.assertEqual(code, res.returncode, "stderr=" + res.stderr)
        return res.stderr

    def calls(self):
        with open(os.path.join(self.stub, "calls")) as f:
            return f.read().splitlines()

    def test_fresh_install_builds_tests_and_starts(self):
        err = self.run_install()
        calls = self.calls()
        self.assertEqual(["make test", "make install-desktop", "systemctl --user daemon-reload",
                          "systemctl --user enable wheelproxy.service"], calls[:4])
        self.assertIn("systemctl --user start wheelproxy.service", calls)
        self.assertNotIn("systemctl --user restart wheelproxy.service", calls)
        self.assertIn("[install] ok pid=200 old_pid=0", err)

    def test_update_with_no_clients_restarts(self):
        self.service("active", 100)
        self.clients(0, others=3)
        err = self.run_install()
        self.assertIn("systemctl --user restart wheelproxy.service", self.calls())
        self.assertIn("[install] ok pid=200 old_pid=100", err)

    def test_connected_clients_defer_the_restart(self):
        self.service("active", 100)
        self.clients(2, others=3)
        err = self.run_install(code=3)
        self.assertNotIn("systemctl --user restart wheelproxy.service", self.calls())
        self.assertIn("make install-desktop", self.calls())
        self.assertIn("[install] deferred reason=clients_connected clients=2 pid=100", err)

    def test_restart_now_restarts_with_clients(self):
        self.service("active", 100)
        self.clients(4)
        err = self.run_install("--restart-now")
        self.assertIn("systemctl --user restart wheelproxy.service", self.calls())
        self.assertIn("[install] ok pid=200 old_pid=100 clients_dropped=4", err)

    def test_failing_tests_install_nothing(self):
        err = self.run_install(code=1, env={"FAIL_MAKE": "test"})
        self.assertEqual(["make test"], self.calls())
        self.assertIn("[install] refused reason=test status=4", err)

    def test_failed_install_step_stops_before_the_service(self):
        err = self.run_install(code=1, env={"FAIL_MAKE": "install-desktop"})
        self.assertFalse([c for c in self.calls() if c.startswith("systemctl")], self.calls())
        self.assertIn("[install] refused reason=install status=4", err)

    def test_service_that_never_starts_is_reported(self):
        start = time.monotonic()
        err = self.run_install(code=1, env={"NO_START": "1"})
        self.assertLess(time.monotonic() - start, 15)
        self.assertIn("[install] refused reason=not_running pid=0 old_pid=0 state=inactive", err)

    def test_restart_that_keeps_the_old_process_is_reported(self):
        self.service("active", 100)
        err = self.run_install(code=1, env={"NO_START": "1"})
        self.assertIn("[install] refused reason=not_running pid=100 old_pid=100 state=active", err)

    def test_unknown_flag_is_refused(self):
        err = self.run_install("--now", code=2)
        self.assertIn("[install] refused reason=unknown_flag flag=--now", err)
        self.assertFalse(os.path.exists(os.path.join(self.stub, "calls")))


if __name__ == "__main__":
    unittest.main()
