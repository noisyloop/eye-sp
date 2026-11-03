"""End-to-end test: builds the namespace lab and checks the ISP really works.

Needs root plus iproute2, nftables, dnsmasq and busybox; skipped otherwise.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from eyesp import cli

TOOLS = ("ip", "nft", "tc", "dnsmasq", "busybox", "curl")
SKIP = (os.geteuid() != 0 or any(shutil.which(t) is None for t in TOOLS)
        or os.path.exists("/run/netns/eyesp-core"))


def eyesp(*args) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = cli.main(list(args))
    if rc:
        raise AssertionError(f"eyesp {' '.join(args)} failed: {out.getvalue()}")
    return out.getvalue()


def as_customer(n: int, *argv, timeout=20) -> subprocess.CompletedProcess:
    return subprocess.run(["ip", "netns", "exec", f"eyesp-c{n}", *argv],
                          capture_output=True, text=True, timeout=timeout)


def fetch(n: int, path="/", max_time=5) -> subprocess.CompletedProcess:
    return as_customer(n, "curl", "-sS", "-m", str(max_time), "-o", "/dev/null",
                       "-w", "%{speed_download}", f"http://web.eyesp.test{path}")


UPLOAD = """
import socket, sys, time
srv = sys.argv[1] == "srv"
if srv:
    s = socket.create_server(("198.51.100.10", 9000)); c, _ = s.accept(); n = 0; t = time.time()
    while time.time() - t < 3:
        b = c.recv(65536)
        if not b: break
        n += len(b)
    print(n * 8 / (time.time() - t) / 1e6)
else:
    c = socket.create_connection(("198.51.100.10", 9000)); c.settimeout(5)
    try:
        while True: c.sendall(b"x" * 65536)
    except OSError: pass
"""


@unittest.skipIf(SKIP, "needs root, lab tools, and no running lab")
class TestLab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["EYESP_STATE"] = cls.tmp
        eyesp("lab", "up")
        eyesp("sub", "add", "alice", "--plan", "starter")   # 10/2 Mbps
        eyesp("sub", "add", "bob", "--plan", "fiber")       # 100/20 Mbps
        eyesp("lab", "connect")

    @classmethod
    def tearDownClass(cls):
        eyesp("lab", "down")
        shutil.rmtree(cls.tmp, ignore_errors=True)
        del os.environ["EYESP_STATE"]

    def test_dhcp_dns_nat(self):
        r = as_customer(1, "curl", "-sS", "-m", "5", "http://web.eyesp.test/")
        self.assertIn("Hello from the internet", r.stdout, r.stderr)
        log = open(os.path.join(self.tmp, "web.log")).read()
        self.assertIn("203.0.113.2", log)  # the internet only ever sees the ISP's public IP

    def test_download_shaping(self):
        alice = float(fetch(1, "/speedtest.bin", 20).stdout) * 8 / 1e6
        bob = float(fetch(2, "/speedtest.bin", 20).stdout) * 8 / 1e6
        self.assertTrue(7 < alice < 11, alice)
        self.assertGreater(bob, 50)

    def test_upload_shaping(self):
        server = subprocess.Popen(["ip", "netns", "exec", "eyesp-inet", sys.executable, "-c", UPLOAD, "srv"],
                                  stdout=subprocess.PIPE, text=True)
        time.sleep(0.5)
        client = subprocess.Popen(["ip", "netns", "exec", "eyesp-c1", sys.executable, "-c", UPLOAD, "cli"])
        rate = float(server.communicate(timeout=15)[0])
        client.kill()
        client.wait()
        self.assertTrue(1.2 < rate < 2.3, rate)

    def test_customers_isolated(self):
        r = as_customer(1, sys.executable, "-c",
                        "import socket; socket.create_connection(('100.64.0.11', 53), 2)")
        self.assertNotEqual(r.returncode, 0)

    def test_suspend_and_resume(self):
        eyesp("sub", "suspend", "bob")
        try:
            self.assertNotEqual(fetch(2, max_time=2).returncode, 0)
        finally:
            eyesp("sub", "resume", "bob")
        self.assertEqual(fetch(2).returncode, 0)

    def test_usage_and_billing(self):
        fetch(1, "/speedtest.bin", 20)
        usage = eyesp("usage")
        line = next(l for l in usage.splitlines() if l.startswith("alice"))
        self.assertRegex(line, r"[4-9]\.\d MB|\d\d\.\d MB")
        self.assertIn("$85.00", eyesp("bill"))


if __name__ == "__main__":
    unittest.main()
