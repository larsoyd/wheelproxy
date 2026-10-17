import array
import os
import resource
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "build", "wheelproxy")

POINTER = 4
WHEEL, FINGER = 0, 1
VERTICAL, HORIZONTAL = 0, 1


STRACE = ["strace", "-f", "-qq", "-o", "/dev/null"]


def msg(obj, op, *args):
    body = b""
    for a in args:
        if isinstance(a, str):
            s = a.encode() + b"\0"
            body += struct.pack("<I", len(s)) + s + b"\0" * (-len(s) % 4)
        else:
            body += struct.pack("<i" if a < 0 else "<I", a)
    return struct.pack("<II", obj, (8 + len(body)) << 16 | op) + body


def parse(data):
    out = []
    while len(data) >= 8:
        obj, word = struct.unpack_from("<II", data)
        size = word >> 16
        out.append((obj, word & 0xFFFF, data[8:size]))
        data = data[size:]
    return out


def wheel_frame(n, axis=VERTICAL, source=WHEEL):
    return (msg(POINTER, 6, source) + msg(POINTER, 9, axis, n)
            + msg(POINTER, 4, 1000, axis, n * 256 // 8) + msg(POINTER, 5))


def send_fds(sock, data, fds):
    sock.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])


def recv_fds(sock, size=65536):
    fds = array.array("i")
    data, anc, _, _ = sock.recvmsg(size, socket.CMSG_SPACE(28 * fds.itemsize))
    for level, kind, payload in anc:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.frombytes(payload[: len(payload) - len(payload) % fds.itemsize])
    return data, list(fds)


def read_for(sock, seconds, quiet=None):
    sock.settimeout(0.005)
    end = time.monotonic() + seconds
    last = time.monotonic()
    chunks = []
    while time.monotonic() < end:
        try:
            data = sock.recv(65536)
        except socket.timeout:
            if quiet and chunks and time.monotonic() - last > quiet:
                break
            continue
        if not data:
            break
        chunks.append((time.monotonic(), data))
        last = time.monotonic()
    return chunks


def read_exact(sock, size, seconds=5):
    sock.settimeout(seconds)
    buf = b""
    while len(buf) < size:
        data = sock.recv(size - len(buf))
        if not data:
            break
        buf += data
    return buf


def timed_events(chunks):
    events = []
    pending = b""
    for t, data in chunks:
        pending += data
        for obj, op, body in parse(pending):
            events.append((t, obj, op, body))
            pending = pending[8 + len(body):]
    return events


def v120_values(events, obj=POINTER):
    return [struct.unpack_from("<Ii", b)[1] for _, o, op, b in events if o == obj and op == 9]


def build():
    res = subprocess.run(["make", "-C", ROOT], capture_output=True, text=True, timeout=120)
    if res.returncode:
        raise AssertionError("build failed: " + res.stdout + res.stderr)


class Proxy(unittest.TestCase):
    args = ["--mult", "2", "--tau-ms", "30", "--tick-ms", "4"]
    upstream_name = "wayland-up"
    seat_version = 9
    lazy = False

    @classmethod
    def setUpClass(cls):
        build()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp")
        self.upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.upstream.bind(os.path.join(self.tmp, "wayland-up"))
        self.upstream.listen(4)
        self.log = open(os.path.join(self.tmp, "log"), "a+")
        self.proc = None
        self.socks = []
        self.client = self.server = None
        if self.lazy:
            return
        self.start()
        self.client = self.dial()
        if self.upstream_name == "wayland-up":
            self.server = self.accept()
            self.handshake(self.client, self.server)

    def tearDown(self):
        self.stop()
        for s in self.socks + [self.upstream]:
            s.close()
        self.log.close()

    def start(self, wrap=()):
        env = dict(os.environ, XDG_RUNTIME_DIR=self.tmp)
        cmd = [*wrap, BIN, "--listen", "wayland-px", "--upstream", self.upstream_name] + self.args
        self.proc = subprocess.Popen(cmd, env=env, stderr=self.log, start_new_session=True)
        path = os.path.join(self.tmp, "wayland-px")
        for _ in range(500):
            if os.path.exists(path):
                return
            time.sleep(0.01)
        self.fail("proxy socket never appeared log=" + self.logtext())

    def stop(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
        path = os.path.join(self.tmp, "wayland-px")
        if os.path.exists(path):
            os.unlink(path)

    def dial(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socks.append(s)
        s.connect(os.path.join(self.tmp, "wayland-px"))
        return s

    def accept(self):
        self.upstream.settimeout(5)
        s, _ = self.upstream.accept()
        self.socks.append(s)
        return s

    def logtext(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()

    def wait_log(self, text, seconds=3):
        deadline = time.monotonic() + seconds
        while text not in self.logtext() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn(text, self.logtext())

    def closed(self, sock, seconds=3):
        sock.settimeout(seconds)
        try:
            while sock.recv(65536):
                pass
        except socket.timeout:
            return False
        except ConnectionResetError:
            pass
        return True

    def handshake(self, client, server):
        setup = msg(1, 1, 2) + msg(2, 0, 7, "wl_seat", self.seat_version, 3) + msg(3, 0, POINTER)
        client.sendall(setup)
        self.assertEqual(setup, read_exact(server, len(setup)), "log=" + self.logtext())

    def events_after(self, data, seconds=1.0, quiet=0.15):
        self.server.sendall(data)
        return timed_events(read_for(self.client, seconds, quiet))


class Passthrough(Proxy):
    def test_request_fd_reaches_compositor(self):
        r, w = os.pipe()
        send_fds(self.client, msg(5, 0, 1), [w])
        os.close(w)
        self.server.settimeout(5)
        data, fds = recv_fds(self.server)
        self.assertEqual(msg(5, 0, 1), data)
        self.assertEqual(1, len(fds))
        os.write(fds[0], b"ok")
        os.close(fds[0])
        self.assertEqual(b"ok", os.read(r, 2))
        os.close(r)

    def test_event_fd_reaches_client(self):
        r, w = os.pipe()
        send_fds(self.server, msg(9, 3, 7), [w])
        os.close(w)
        self.client.settimeout(5)
        data, fds = recv_fds(self.client)
        self.assertEqual(msg(9, 3, 7), data)
        os.write(fds[0], b"hi")
        os.close(fds[0])
        self.assertEqual(b"hi", os.read(r, 2))
        os.close(r)

    def test_more_fds_than_a_message_carries_is_refused(self):
        r, w = os.pipe()
        send_fds(self.client, msg(5, 0, 1), [w] * 40)
        os.close(w)
        os.close(r)
        self.assertTrue(self.closed(self.client), "log=" + self.logtext())
        self.assertIn("[conn] close id=1 reason=fd_truncated", self.logtext())

    def test_large_burst_both_ways_is_byte_exact(self):
        down = b"".join(msg(1000 + i % 50, i % 7, i, "x" * (i % 40)) for i in range(4000))
        up = b"".join(msg(2000 + i % 50, i % 5, i) for i in range(4000))
        self.server.sendall(down)
        self.client.sendall(up)
        time.sleep(0.2)
        self.assertEqual(up, read_exact(self.server, len(up)))
        self.assertEqual(down, read_exact(self.client, len(down)))

    def test_finger_scroll_is_untouched(self):
        frame = msg(POINTER, 6, FINGER) + msg(POINTER, 4, 5, VERTICAL, 2560) + msg(POINTER, 5)
        self.server.sendall(frame)
        self.assertEqual(frame, read_exact(self.client, len(frame)))
        self.assertEqual([], read_for(self.client, 0.1))

    def test_horizontal_wheel_is_untouched(self):
        frame = wheel_frame(120, axis=HORIZONTAL)
        self.server.sendall(frame)
        self.assertEqual(frame, read_exact(self.client, len(frame)))
        self.assertEqual([], read_for(self.client, 0.1))

    def test_value120_on_other_object_is_untouched(self):
        ev = msg(3, 9, VERTICAL, 120)
        self.server.sendall(ev)
        self.assertEqual(ev, read_exact(self.client, len(ev)))

    def test_deleted_pointer_id_is_untracked(self):
        self.server.sendall(msg(1, 1, POINTER))
        self.assertEqual(msg(1, 1, POINTER), read_exact(self.client, 12))
        rebind = msg(2, 0, 8, "wl_output", 4, POINTER)
        self.client.sendall(rebind)
        self.assertEqual(rebind, read_exact(self.server, len(rebind)))
        frame = wheel_frame(120)
        self.server.sendall(frame)
        self.assertEqual(frame, read_exact(self.client, len(frame)))


    def test_only_wl_seat_binds_are_seats(self):
        bind = msg(2, 0, 9, "wl_output", 9, 20) + msg(20, 0, 51)
        self.client.sendall(bind)
        self.assertEqual(bind, read_exact(self.server, len(bind)))
        frame = msg(51, 9, VERTICAL, 120) + msg(51, 5)
        self.server.sendall(frame)
        self.assertEqual(frame, read_exact(self.client, len(frame)))


class Lifecycle(Proxy):
    def test_start_line_names_the_pid(self):
        self.wait_log("[wheelproxy] start pid=%d " % self.proc.pid)

    def test_hangup_close_line_has_errno_age_and_queues(self):
        self.client.close()
        self.wait_log("[conn] close id=1 reason=client_hangup errno=none ms=")
        log = self.logtext()
        self.assertIn("queued_up=0 queued_down=0 fds_up=0 fds_down=0 notches=0 retries=0", log)
        self.assertLess(int(log.split(" ms=")[1].split()[0]), 5000, log)

    def test_sigterm_logs_why_it_stopped(self):
        os.kill(self.proc.pid, signal.SIGTERM)
        self.assertEqual(0, self.proc.wait(timeout=5))
        log = self.logtext()
        self.assertIn("[conn] close id=1 reason=shutdown errno=none", log)
        self.assertIn("[wheelproxy] stop reason=SIGTERM served=1", log)


class Debug(Proxy):
    def test_usr1_dumps_connection_state(self):
        os.kill(self.proc.pid, signal.SIGUSR1)
        self.wait_log("[state] conns=1 served=1")
        log = self.logtext()
        self.assertIn("queued_up=0 queued_down=0 fds_up=0 fds_down=0 pointers=1 ticking=0 connecting=0 retries=0",
                      log)
        self.assertIn("[state] conn id=1 ms=", log)
        self.assertIn("[state] pointer id=1 pointer=4 version=9 remaining=0.0 notches=0", log)
        self.client.sendall(msg(5, 0, 1))
        self.assertEqual(msg(5, 0, 1), read_exact(self.server, 12))

    def test_pointer_past_the_limit_is_logged(self):
        reqs = b"".join(msg(3, 0, 60 + i) for i in range(8))
        self.client.sendall(reqs)
        self.assertEqual(reqs, read_exact(self.server, len(reqs)))
        self.wait_log("[seat] untracked id=1 seat=3 pointer=67 reason=pointer_limit max=8")

    def test_malformed_bind_is_logged(self):
        for length in (0, 1000, 0xFFFFFFFD):
            bad = msg(2, 0, 7, length, 9, 30)
            self.client.sendall(bad)
            self.assertEqual(bad, read_exact(self.server, len(bad)))
            self.wait_log("[proto] warn id=1 reason=bad_bind len=%d size=24" % length)

    def test_huge_object_id_is_refused(self):
        self.client.sendall(msg(1, 1, 1 << 23))
        self.assertTrue(self.closed(self.client), "log=" + self.logtext())
        log = self.logtext()
        self.assertIn("[proto] error id=1 reason=id_range obj=8388608 max=4194303", log)
        self.assertIn("[conn] close id=1 reason=id_range", log)

    def test_largest_allowed_id_is_tracked(self):
        bind = msg(2, 0, 7, "wl_seat", 9, (1 << 22) - 1)
        self.client.sendall(bind)
        self.assertEqual(bind, read_exact(self.server, len(bind)))
        self.assertNotIn("id_range", self.logtext())


class OldSeat(Proxy):
    seat_version = 7

    def test_pointer_below_v8_is_untouched(self):
        frame = wheel_frame(120)
        self.server.sendall(frame)
        self.assertEqual(frame, read_exact(self.client, len(frame)))
        self.assertEqual([], read_for(self.client, 0.1))


class Smoothing(Proxy):
    def test_notch_is_scaled_and_spread_over_frames(self):
        sent = time.monotonic()
        events = self.events_after(wheel_frame(90))
        values = v120_values(events)
        self.assertEqual(180, sum(values), "values=%r" % values)
        self.assertGreaterEqual(len(values), 5, "values=%r" % values)
        first = next(t for t, o, op, _ in events if o == POINTER and op == 9)
        self.assertLess(first - sent, 0.02)
        last = max(t for t, o, op, _ in events if o == POINTER and op == 9)
        self.assertLess(last - sent, 0.6)

    def test_first_step_is_sent_with_the_original_frame(self):
        self.server.sendall(wheel_frame(120))
        chunks = read_for(self.client, 0.5, quiet=0.15)
        first = [op for o, op, _ in parse(chunks[0][1]) if o == POINTER]
        self.assertIn(9, first, "first=%r" % first)

    def test_reused_seat_id_is_not_a_seat(self):
        self.server.sendall(msg(1, 1, 3))
        self.assertEqual(msg(1, 1, 3), read_exact(self.client, 12))
        reuse = msg(10, 0, 3) + msg(3, 0, 50)
        self.client.sendall(reuse)
        self.assertEqual(reuse, read_exact(self.server, len(reuse)))
        frame = (msg(50, 6, WHEEL) + msg(50, 9, VERTICAL, 120)
                 + msg(50, 4, 1, VERTICAL, 3840) + msg(50, 5))
        self.server.sendall(frame)
        self.assertEqual(frame, read_exact(self.client, len(frame)))

    def test_steps_are_front_loaded(self):
        values = v120_values(self.events_after(wheel_frame(120)))
        self.assertEqual(values[0], max(values), "values=%r" % values)
        for a, b in zip(values, values[1:]):
            self.assertLessEqual(b, a + 1, "values=%r" % values)

    def test_every_step_is_a_complete_frame_with_timestamp(self):
        events = [(o, op, b) for _, o, op, b in self.events_after(wheel_frame(120)) if o == POINTER]
        steps = 0
        last_time = 1000
        for i, (o, op, b) in enumerate(events):
            if op != 9:
                continue
            steps += 1
            rest = [e[1] for e in events[i + 1:]]
            self.assertEqual(4, rest[0], "events=%r" % events)
            n = struct.unpack_from("<Ii", b)[1]
            when, axis, value = struct.unpack_from("<IIi", events[i + 1][2])
            self.assertEqual(VERTICAL, axis)
            self.assertEqual(n * 32, value, "a v120 step of %d is %d/8 of a 15 unit detent" % (n, n))
            self.assertGreaterEqual(when, last_time)
            last_time = when
            self.assertEqual(5, rest[1], "events=%r" % events)
        self.assertGreater(steps, 0)
        self.assertGreater(last_time, 1000)

    def test_original_vertical_events_are_absorbed(self):
        ops = [op for _, o, op, _ in self.events_after(wheel_frame(120)) if o == POINTER]
        self.assertNotIn(9, ops[: ops.index(5)], "ops=%r" % ops)
        self.assertNotIn(4, ops[: ops.index(5)], "ops=%r" % ops)

    def test_consecutive_notches_accumulate(self):
        self.server.sendall(wheel_frame(90))
        time.sleep(0.01)
        values = v120_values(self.events_after(wheel_frame(90)))
        self.assertEqual(360, sum(values), "values=%r" % values)

    def test_split_message_is_reassembled(self):
        frame = wheel_frame(120)
        self.server.sendall(frame[:22])
        time.sleep(0.03)
        values = v120_values(self.events_after(frame[22:]))
        self.assertEqual(240, sum(values), "values=%r" % values)

    def test_reversal_drops_the_rest(self):
        self.server.sendall(wheel_frame(120))
        early = timed_events(read_for(self.client, 0.02))
        values = v120_values(early) + v120_values(self.events_after(wheel_frame(-120)))
        neg = [v for v in values if v < 0]
        self.assertEqual(-240, sum(neg), "values=%r" % values)
        first_neg = values.index(neg[0])
        self.assertTrue(all(v < 0 for v in values[first_neg:]), "values=%r" % values)
        self.assertLess(sum(v for v in values if v > 0), 240, "values=%r" % values)

    def test_leave_stops_scrolling(self):
        self.server.sendall(wheel_frame(120))
        early = timed_events(read_for(self.client, 0.012))
        events = early + self.events_after(msg(POINTER, 1, 9, 77) + msg(POINTER, 5))
        ops = [(op, b) for _, o, op, b in events if o == POINTER]
        leave = ops.index((1, struct.pack("<II", 9, 77)))
        self.assertNotIn(9, [op for op, _ in ops[leave:]], "ops=%r" % ops)
        self.assertLess(sum(v120_values(events)), 240)

    def test_release_stops_scrolling_and_untracks_the_pointer(self):
        self.server.sendall(wheel_frame(120))
        early = timed_events(read_for(self.client, 0.012))
        self.client.sendall(msg(POINTER, 1))
        self.assertEqual(msg(POINTER, 1), read_exact(self.server, 8), "log=" + self.logtext())
        marker = msg(9, 3, 7)
        events = self.events_after(marker)
        objs = [(o, op) for _, o, op, _ in events]
        after = objs[objs.index((9, 3)) + 1:]
        self.assertNotIn((POINTER, 9), after, "early=%d after=%r" % (len(early), after))
        self.assertLess(sum(v120_values(early + events)), 240)
        frame = wheel_frame(120)
        self.server.sendall(frame)
        self.assertEqual(frame, read_exact(self.client, len(frame)), "log=" + self.logtext())

    def test_set_cursor_keeps_the_pointer_tracked(self):
        cursor = msg(POINTER, 0, 5, 0, 0, 0)
        self.client.sendall(cursor)
        self.assertEqual(cursor, read_exact(self.server, len(cursor)))
        values = v120_values(self.events_after(wheel_frame(120)))
        self.assertEqual(240, sum(values), "values=%r log=%s" % (values, self.logtext()))
        self.assertGreater(len(values), 1, "values=%r" % values)


class Verbose(Proxy):
    args = Proxy.args + ["--verbose"]

    def test_animation_settles_and_logs_it(self):
        self.events_after(wheel_frame(90))
        deadline = time.monotonic() + 1
        while "[wheel] settled" not in self.logtext() and time.monotonic() < deadline:
            time.sleep(0.02)
        log = self.logtext()
        self.assertIn("[wheel] pointer=4 v120=90 remaining=180.0", log)
        self.assertIn("[wheel] settled pointer=4", log, "log=" + log)


    def test_seat_and_pointer_tracking_is_logged(self):
        self.wait_log("[seat] bind id=1 seat=3 version=9")
        self.assertIn("[pointer] track id=1 pointer=4 version=9", self.logtext())
        self.server.sendall(msg(1, 1, POINTER))
        self.assertEqual(msg(1, 1, POINTER), read_exact(self.client, 12))
        self.wait_log("[pointer] drop id=1 pointer=4 reason=delete_id")

    def test_release_is_logged(self):
        self.client.sendall(msg(POINTER, 1))
        self.assertEqual(msg(POINTER, 1), read_exact(self.server, 8))
        self.wait_log("[pointer] drop id=1 pointer=4 reason=release")

    def test_timer_stops_after_settling(self):
        self.events_after(wheel_frame(90))
        time.sleep(0.6)
        before = self.switches()
        time.sleep(0.5)
        self.assertLess(self.switches() - before, 10)

    def switches(self):
        with open("/proc/%d/status" % self.proc.pid) as f:
            return int(next(l for l in f if l.startswith("voluntary_ctxt_switches")).split()[1])


class Backpressure(Proxy):
    def stall(self, src, dst):
        block = b"".join(msg(1000, 1, i, "y" * 200) for i in range(256))
        reps = (48 << 20) // len(block)
        sender = threading.Thread(target=lambda: [src.sendall(block) for _ in range(reps)])
        sender.start()
        time.sleep(0.5)
        with open("/proc/%d/status" % self.proc.pid) as f:
            hwm = int(next(l for l in f if l.startswith("VmHWM")).split()[1])
        got = read_exact(dst, reps * len(block), seconds=20)
        sender.join(timeout=20)
        self.assertFalse(sender.is_alive())
        self.assertEqual(block * reps, got)
        self.assertLess(hwm, 24 << 10, "proxy peak rss kb=%d while reader stalled" % hwm)

    def test_fds_queued_behind_a_stalled_client_all_arrive(self):
        filler = b"".join(msg(1000, 1, i, "z" * 200) for i in range(1500))
        self.server.sendall(filler)
        time.sleep(0.2)
        r, w = os.pipe()
        for i in range(10):
            send_fds(self.server, msg(9, 3, i), [w] * 25)
            time.sleep(0.01)
        os.close(w)
        expect = filler + b"".join(msg(9, 3, i) for i in range(10))
        got, fds = b"", 0
        self.client.settimeout(5)
        while len(got) < len(expect):
            data, received = recv_fds(self.client)
            if not data:
                break
            got += data
            fds += len(received)
            for fd in received:
                os.close(fd)
        os.close(r)
        self.assertEqual(expect, got)
        self.assertEqual(250, fds, "fds delivered with the data log=" + self.logtext())

    def test_stalled_client_does_not_grow_the_proxy(self):
        self.stall(self.server, self.client)

    def test_stalled_compositor_does_not_grow_the_proxy(self):
        self.stall(self.client, self.server)


class Slower(Proxy):
    args = ["--mult", "1", "--tau-ms", "80", "--tick-ms", "8"]

    def test_parameters_change_the_curve(self):
        sent = time.monotonic()
        events = self.events_after(wheel_frame(120), seconds=1.5, quiet=0.3)
        values = v120_values(events)
        self.assertEqual(120, sum(values), "values=%r" % values)
        last = max(t for t, o, op, _ in events if o == POINTER and op == 9)
        self.assertGreater(last - sent, 0.15)


class Defaults(Proxy):
    args = []

    def test_default_distance_and_curve(self):
        sent = time.monotonic()
        events = self.events_after(wheel_frame(120), seconds=1.5, quiet=0.3)
        values = v120_values(events)
        self.assertEqual(198, sum(values), "values=%r" % values)
        times = [t for t, o, op, _ in events if o == POINTER and op == 9]
        cum, half = 0, None
        for t, v in zip(times, values):
            cum += v
            if half is None and cum >= 99:
                half = t - sent
        self.assertGreater(half, 0.035, "half=%.3f" % half)
        self.assertLess(half, 0.075, "half=%.3f" % half)


    def steps(self, notch=120):
        events = [(o, op, b) for _, o, op, b in self.events_after(wheel_frame(notch), seconds=1.5, quiet=0.3)
                  if o == POINTER]
        out = []
        for i, (o, op, b) in enumerate(events):
            if op == 9:
                n = struct.unpack_from("<Ii", b)[1]
                out.append((struct.unpack_from("<I", events[i + 1][2])[0], n))
        return out

    def test_tail_decelerates_to_a_stop(self):
        steps = self.steps()
        reached, total = [], 0
        for t, n in steps:
            for _ in range(n):
                total += 1
                reached.append(t)
        self.assertEqual(198, total)
        early = reached[-8] - reached[-11]
        late = reached[-1] - reached[-4]
        self.assertGreater(late, 1.8 * early, "ms for 3px early=%d late=%d steps=%r" % (early, late, steps))

    def test_notch_finishes_without_a_long_tail(self):
        times = [t for t, _ in self.steps()]
        self.assertLess(times[-1] - times[0], 330, "first=%d last=%d" % (times[0], times[-1]))

    def test_steps_are_finer_than_a_display_frame(self):
        times = [t for t, _ in self.steps()]
        first = [b - a for a, b in zip(times, times[1:]) if b - times[0] < 100]
        first.sort()
        self.assertLessEqual(first[len(first) // 2], 2, "intervals=%r" % first)


class MissingUpstream(Proxy):
    upstream_name = "wayland-gone"

    def test_client_is_refused_with_reason(self):
        self.client.settimeout(5)
        self.assertEqual(b"", self.client.recv(16))
        log = self.logtext()
        self.assertIn("[conn] refused id=1 reason=upstream_connect errno=ENOENT", log)
        self.assertIn("wayland-gone", log)


def cpu_seconds(pid):
    with open("/proc/%d/stat" % pid) as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


class FdLimit(Proxy):
    def test_exhausted_fds_refuse_the_client_without_spinning(self):
        pid = self.proc.pid
        used = len(os.listdir("/proc/%d/fd" % pid))
        soft, hard = resource.prlimit(pid, resource.RLIMIT_NOFILE)
        resource.prlimit(pid, resource.RLIMIT_NOFILE, (used, hard))
        late = self.dial()
        time.sleep(0.3)
        before = cpu_seconds(pid)
        time.sleep(0.5)
        burned = cpu_seconds(pid) - before
        self.assertLess(burned, 0.1, "cpu seconds in 0.5 s while out of fds=%.2f" % burned)
        self.assertTrue(self.closed(late), "client left waiting log=" + self.logtext())
        self.assertIn("[conn] refused reason=accept errno=EMFILE", self.logtext())
        resource.prlimit(pid, resource.RLIMIT_NOFILE, (soft, hard))
        self.handshake(self.dial(), self.accept())


class MemoryLimit(Proxy):
    def test_failed_allocation_refuses_the_client(self):
        pid = self.proc.pid
        with open("/proc/%d/status" % pid) as f:
            vm = int(next(l for l in f if l.startswith("VmSize")).split()[1]) * 1024
        resource.prlimit(pid, resource.RLIMIT_AS, (vm + (64 << 10), resource.RLIM_INFINITY))
        late = self.dial()
        self.assertTrue(self.closed(late), "log=" + self.logtext())
        resource.prlimit(pid, resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
        self.wait_log("[conn] refused reason=out_of_memory")
        self.client.sendall(msg(5, 0, 1))
        self.assertEqual(msg(5, 0, 1), read_exact(self.server, 12))


class FullBacklog(Proxy):
    def fill(self):
        path = os.path.join(self.tmp, "wayland-up")
        queued = 0
        for _ in range(64):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socks.append(s)
            s.setblocking(False)
            try:
                s.connect(path)
            except BlockingIOError:
                return queued
            queued += 1
        self.fail("compositor backlog never filled")

    def drain(self, queued):
        for _ in range(queued):
            self.accept()

    def retries(self):
        return self.logtext().count("[conn] retry id=2 ")

    def test_full_compositor_backlog_does_not_stall_other_clients(self):
        self.fill()
        late = self.dial()
        time.sleep(0.1)
        self.client.sendall(msg(5, 0, 1))
        try:
            got = read_exact(self.server, 12, seconds=2)
        except socket.timeout:
            got = b""
        self.assertEqual(msg(5, 0, 1), got, "proxy stalled log=" + self.logtext())
        start = time.monotonic()
        self.assertTrue(self.closed(late, seconds=5), "log=" + self.logtext())
        self.assertGreater(time.monotonic() - start, 1.5, "gave up early log=" + self.logtext())
        self.assertIn("[conn] refused id=2 reason=upstream_connect errno=EAGAIN retries=11 ms=", self.logtext())
        self.assertIn("[conn] retry id=2 reason=upstream_connect errno=EAGAIN attempt=11 delay_ms=1024",
                      self.logtext())

    def test_waiting_client_connects_once_the_backlog_drains(self):
        queued = self.fill()
        late = self.dial()
        setup = msg(1, 1, 2) + msg(2, 0, 7, "wl_seat", 9, 3) + msg(3, 0, POINTER)
        late.sendall(setup)
        self.wait_log("[conn] retry id=2 reason=upstream_connect errno=EAGAIN attempt=1 delay_ms=1")
        before = cpu_seconds(self.proc.pid)
        time.sleep(0.3)
        burned = cpu_seconds(self.proc.pid) - before
        self.assertLess(burned, 0.1, "cpu seconds in 0.3 s while waiting=%.2f" % burned)
        os.kill(self.proc.pid, signal.SIGUSR1)
        self.wait_log("[state] conns=2 served=1")
        self.assertRegex(self.logtext(), r"\[state\] conn id=2 .* connecting=1 retries=\d+")
        self.drain(queued)
        server = self.accept()
        self.assertEqual(setup, read_exact(server, len(setup)), "log=" + self.logtext())
        self.wait_log("[conn] open id=2 retries=")
        server.sendall(wheel_frame(120))
        self.assertEqual(240, sum(v120_values(timed_events(read_for(late, 1.0, 0.15)))), "log=" + self.logtext())

    def test_client_that_leaves_while_waiting_is_closed(self):
        queued = self.fill()
        late = self.dial()
        self.wait_log("[conn] retry id=2 ")
        late.sendall(msg(1, 1, 2))
        late.close()
        self.wait_log("[conn] close id=2 reason=client_hangup errno=none")
        self.assertRegex(self.logtext(), r"\[conn\] close id=2 .* retries=[1-9]")
        seen = self.retries()
        self.drain(queued)
        time.sleep(0.3)
        self.assertEqual(seen, self.retries(), "kept retrying log=" + self.logtext())
        self.upstream.settimeout(0.2)
        self.assertRaises(socket.timeout, self.upstream.accept)

    def test_compositor_gone_while_waiting_refuses_with_its_errno(self):
        self.fill()
        late = self.dial()
        self.wait_log("[conn] retry id=2 ")
        self.upstream.close()
        self.assertTrue(self.closed(late), "log=" + self.logtext())
        self.assertIn("[conn] refused id=2 reason=upstream_connect errno=ECONNREFUSED retries=", self.logtext())
        self.assertLess(self.retries(), 11, "log=" + self.logtext())


class Faults(Proxy):
    lazy = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not shutil.which("strace"):
            raise AssertionError("strace is required for the fault injection tests")

    def inject(self, *spec):
        self.start(STRACE + list(spec))

    def attached(self):
        client, server = self.dial(), self.accept()
        self.handshake(client, server)
        return client, server

    def assert_refused(self, client, line):
        self.assertTrue(self.closed(client), "client left open log=" + self.logtext())
        self.wait_log(line)

    def test_accept_error_is_logged_and_retried(self):
        self.inject("--inject=accept4:error=ECONNABORTED:when=1")
        self.attached()
        self.assertIn("[conn] refused reason=accept errno=ECONNABORTED", self.logtext())

    def test_timerfd_failure_refuses_the_client(self):
        self.inject("--inject=timerfd_create:error=EMFILE:when=1")
        self.assert_refused(self.dial(), "[conn] refused id=1 reason=timerfd errno=EMFILE")
        self.attached()

    def test_upstream_socket_failure_refuses_the_client(self):
        self.inject("--inject=socket:error=EMFILE:when=2")
        self.assert_refused(self.dial(), "[conn] refused id=1 reason=upstream_connect errno=EMFILE")
        self.attached()

    def test_epoll_add_failure_refuses_the_client(self):
        for when in (3, 4, 5):
            with self.subTest(when=when):
                self.stop()
                self.inject("--inject=epoll_ctl:error=ENOSPC:when=%d" % when)
                self.assert_refused(self.dial(), "reason=epoll_add errno=ENOSPC")
                self.attached()

    def test_epoll_mod_failure_closes_the_connection(self):
        self.inject("--inject=epoll_ctl:error=ENOMEM:when=6")
        client, server = self.dial(), self.accept()
        client.sendall(msg(1, 1, 2))
        self.assert_refused(client, "[conn] close id=1 reason=epoll_mod errno=ENOMEM")

    def test_timer_arm_failure_closes_the_connection(self):
        self.inject("--inject=timerfd_settime:error=EINVAL:when=1")
        client, server = self.attached()
        server.sendall(wheel_frame(120))
        self.assert_refused(client, "[conn] close id=1 reason=timer_arm errno=EINVAL")

    def test_connect_backlog_is_retried_with_backoff(self):
        self.inject("--inject=connect:error=EAGAIN:when=1..3")
        client = self.dial()
        setup = msg(1, 1, 2) + msg(2, 0, 7, "wl_seat", 9, 3) + msg(3, 0, POINTER)
        client.sendall(setup)
        self.assertEqual(setup, read_exact(self.accept(), len(setup)), "log=" + self.logtext())
        self.wait_log("[conn] open id=1 retries=3 upstream=")
        log = self.logtext()
        for attempt, delay in ((1, 1), (2, 2), (3, 4)):
            self.assertIn("[conn] retry id=1 reason=upstream_connect errno=EAGAIN attempt=%d delay_ms=%d"
                          % (attempt, delay), log)

    def test_retry_timer_failure_refuses_the_client(self):
        self.inject("--inject=connect:error=EAGAIN:when=1", "--inject=timerfd_settime:error=EINVAL:when=1")
        self.assert_refused(self.dial(), "[conn] refused id=1 reason=timer_arm errno=EINVAL retries=1")
        self.attached()

    def test_retry_epoll_failure_refuses_the_client(self):
        self.inject("--inject=connect:error=EAGAIN:when=1", "--inject=epoll_ctl:error=ENOMEM:when=6")
        self.assert_refused(self.dial(), "[conn] refused id=1 reason=epoll_mod errno=ENOMEM retries=1")
        self.attached()

    def test_timer_read_failure_closes_the_connection(self):
        self.inject("-e", "trace=read", "--trace-fds=8,9,10,11,12", "--inject=read:error=EIO")
        client, server = self.attached()
        server.sendall(wheel_frame(120))
        self.assert_refused(client, "[conn] close id=1 reason=timer_read errno=EIO")

    def test_receive_error_is_not_reported_as_hangup(self):
        self.inject("--inject=recvmsg:error=ECONNRESET:when=1")
        client, server = self.dial(), self.accept()
        client.sendall(msg(1, 1, 2))
        self.assert_refused(client, "[conn] close id=1 reason=recv_client errno=ECONNRESET")

    def test_send_error_names_errno(self):
        self.inject("--inject=sendmsg:error=EPIPE:when=1")
        client, server = self.dial(), self.accept()
        client.sendall(msg(1, 1, 2))
        self.assert_refused(client, "[conn] close id=1 reason=send_upstream errno=EPIPE")

    def test_interrupted_wait_is_retried(self):
        self.inject("--inject=epoll_wait:error=EINTR:when=1..3")
        self.attached()
        self.assertNotIn("refused", self.logtext())

    def test_failed_wait_stops_instead_of_spinning(self):
        self.inject("--inject=epoll_wait:error=EBADF:when=2")
        try:
            code = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        self.assertEqual(1, code, "log=" + self.logtext())
        self.assertIn("[loop] refused reason=epoll_wait errno=EBADF", self.logtext())

    def test_failed_signal_read_stops(self):
        self.inject("-e", "trace=read", "--trace-fds=4,5,6,7", "--inject=read:error=EIO")
        self.wait_log("[wheelproxy] start pid=")
        pid = int(self.logtext().split("start pid=")[1].split()[0])
        os.kill(pid, signal.SIGTERM)
        try:
            code = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        self.assertEqual(1, code, "log=" + self.logtext())
        self.assertIn("[loop] refused reason=signal_read errno=EIO", self.logtext())


class Startup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build()

    def refused(self, *spec):
        tmp = tempfile.mkdtemp(prefix="wp")
        env = dict(os.environ, XDG_RUNTIME_DIR=tmp)
        cmd = STRACE + list(spec) + [BIN, "--listen", "wayland-px", "--upstream", "wayland-up"]
        proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            _, err = proc.communicate(timeout=5)
            self.fail("proxy kept running stderr=" + err)
        self.assertEqual(1, proc.returncode, "stderr=" + err)
        self.assertFalse(os.path.exists(os.path.join(tmp, "wayland-px")), "socket left behind")
        return err

    def test_signalfd_failure(self):
        self.assertIn("[setup] refused reason=signalfd errno=ENOMEM",
                      self.refused("--inject=signalfd4:error=ENOMEM"))

    def test_epoll_create_failure(self):
        self.assertIn("[setup] refused reason=epoll_create errno=EMFILE",
                      self.refused("--inject=epoll_create1:error=EMFILE"))

    def test_listener_watch_failure(self):
        self.assertIn("[setup] refused reason=epoll_add errno=ENOSPC",
                      self.refused("--inject=epoll_ctl:error=ENOSPC:when=1"))

    def test_signal_mask_failure(self):
        self.assertIn("[setup] refused reason=sigmask errno=EINVAL",
                      self.refused("--inject=rt_sigprocmask:error=EINVAL:when=1"))

    def test_spare_fd_failure(self):
        self.assertIn("[setup] refused reason=spare_fd errno=EMFILE",
                      self.refused("-e", "trace=openat", "-P", "/dev/null", "--inject=openat:error=EMFILE"))


class Arguments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build()

    def test_bad_number_is_refused(self):
        res = subprocess.run([BIN, "--mult", "abc"], capture_output=True, text=True, timeout=10)
        self.assertEqual(2, res.returncode)
        self.assertIn("[args] refused", res.stderr)
        self.assertIn("--mult", res.stderr)

    def run_in_dir(self, length):
        tmp = tempfile.mkdtemp(prefix="wp")
        deep = tmp + "/" + "d" * (length - len(tmp) - len("/wayland-px") - 1)
        os.mkdir(deep)
        env = dict(os.environ, XDG_RUNTIME_DIR=deep)
        proc = subprocess.Popen([BIN, "--listen", "wayland-px", "--upstream", "wayland-up"], env=env,
                                stderr=subprocess.PIPE, text=True)
        try:
            _, err = proc.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            _, err = proc.communicate(timeout=5)
        self.assertEqual(length, len(os.path.join(deep, "wayland-px")))
        return proc.returncode, err

    def test_path_length_limit_is_exact(self):
        code, err = self.run_in_dir(107)
        self.assertIn("[wheelproxy] start", err)
        code, err = self.run_in_dir(108)
        self.assertEqual(2, code, err)
        self.assertIn("reason=path_too_long", err)

    def test_long_runtime_dir_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="wp" + "x" * 120)
        env = dict(os.environ, XDG_RUNTIME_DIR=tmp)
        try:
            res = subprocess.run([BIN, "--listen", "wayland-px", "--upstream", "wayland-up"], env=env,
                                 capture_output=True, text=True, timeout=3)
        except subprocess.TimeoutExpired:
            self.fail("proxy started on a truncated path")
        self.assertEqual(2, res.returncode, res.stderr)
        self.assertIn("[args] refused reason=path_too_long name=wayland-px max=107", res.stderr)


if __name__ == "__main__":
    unittest.main()
