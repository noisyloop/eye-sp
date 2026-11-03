"""A whole ISP on one Linux box, built from network namespaces.

    [eyesp-cN] customer ---+
    [eyesp-cM] customer ---+-- br-access (100.64.0.1/22)
                               [eyesp-core]  DHCP, DNS, NAT, shaping, metering
                               wan0 (203.0.113.2/30)
                                 |
                               eth0 (203.0.113.1/30)
                               [eyesp-inet]  "the internet": web.eyesp.test = 198.51.100.10
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import render, router
from .config import Config
from .db import DB, Error, Subscriber

INET = "eyesp-inet"
CORE_WAN = "203.0.113.2/30"
INET_WAN = "203.0.113.1/30"
UPSTREAM_GW = "203.0.113.1"
WEB_IP = "198.51.100.10"
LAB_HOSTS = {"web.eyesp.test": WEB_IP}
UDHCPC_SCRIPT = Path(__file__).with_name("udhcpc.sh")


def sh(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(argv, capture_output=True, text=True)
    if check and p.returncode:
        raise Error(f"{' '.join(argv)}: {p.stderr.strip()}")
    return p


def ns_exists(name: str) -> bool:
    return os.path.exists(f"/run/netns/{name}")


def customer_ns(sub: Subscriber) -> str:
    return f"eyesp-c{sub.id}"


def require_tools() -> None:
    if os.geteuid() != 0:
        raise Error("the lab needs root (try: sudo python3 -m eyesp ...)")
    missing = [t for t in ("ip", "nft", "tc", "dnsmasq", "busybox") if not shutil.which(t)]
    if missing:
        raise Error("missing tools: " + ", ".join(missing)
                    + " (Debian/Ubuntu: apt install iproute2 nftables dnsmasq busybox)")


def _spawn(cfg: Config, ns: str, argv: list[str], name: str) -> None:
    log = open(cfg.state_dir / f"{name}.log", "ab")
    p = subprocess.Popen(["ip", "netns", "exec", ns, *argv], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    (cfg.state_dir / f"{name}.pid").write_text(str(p.pid))


def up(cfg: Config, db: DB) -> None:
    require_tools()
    core = cfg.netns or "eyesp-core"
    if ns_exists(core):
        raise Error("lab is already up (run `lab down` first)")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    # The internet, and the ISP's transit link to it.
    sh("ip", "netns", "add", INET)
    sh("ip", "netns", "add", core)
    sh("ip", "link", "add", cfg.wan_if, "netns", core, "type", "veth", "peer", "name", "eth0", "netns", INET)
    sh("ip", "-n", INET, "addr", "add", INET_WAN, "dev", "eth0")
    sh("ip", "-n", INET, "addr", "add", f"{WEB_IP}/32", "dev", "lo")
    for ns, dev in ((INET, "lo"), (INET, "eth0"), (core, "lo"), (core, cfg.wan_if)):
        sh("ip", "-n", ns, "link", "set", dev, "up")

    # ISP core router.
    sh("ip", "-n", core, "addr", "add", CORE_WAN, "dev", cfg.wan_if)
    sh("ip", "-n", core, "route", "add", "default", "via", UPSTREAM_GW)
    sh("ip", "-n", core, "link", "add", cfg.access_if, "type", "bridge")
    sh("ip", "-n", core, "addr", "add", f"{cfg.gateway}/{cfg.access_net.prefixlen}", "dev", cfg.access_if)
    sh("ip", "-n", core, "link", "set", cfg.access_if, "up")
    sh("ip", "netns", "exec", core, "sysctl", "-qw", "net.ipv4.ip_forward=1")

    # A web server on "the internet", with a file for speed tests.
    www = cfg.state_dir / "www"
    www.mkdir(exist_ok=True)
    (www / "index.html").write_text("<h1>Hello from the internet!</h1>\n")
    (www / "speedtest.bin").write_bytes(os.urandom(4 * 1024 * 1024))
    _spawn(cfg, INET, [sys.executable, "-m", "http.server", "--bind", WEB_IP,
                       "--directory", str(www), "80"], "web")

    # DHCP + DNS.
    conf = cfg.state_dir / "dnsmasq.conf"
    conf.write_text(render.dnsmasq_conf(cfg, LAB_HOSTS))
    cfg.dhcp_hosts_path.write_text("")
    sh("ip", "netns", "exec", core, "dnsmasq", f"--conf-file={conf}")

    router.apply(cfg, db)
    probe = f"import socket; socket.create_connection(('{WEB_IP}', 80), 1).close()"
    for _ in range(50):  # wait for the web server to listen
        if sh("ip", "netns", "exec", INET, sys.executable, "-c", probe, check=False).returncode == 0:
            break
        time.sleep(0.1)


def connect(cfg: Config, sub: Subscriber) -> str:
    """Plug a customer's router (CPE) into the access network and run DHCP."""
    require_tools()
    core = cfg.netns
    if not ns_exists(core):
        raise Error("lab is not up")
    ns = customer_ns(sub)
    port = f"c{sub.id}"
    if not ns_exists(ns):
        sh("ip", "netns", "add", ns)
        sh("ip", "link", "add", port, "netns", core, "type", "veth", "peer", "name", "eth0", "netns", ns)
        sh("ip", "-n", ns, "link", "set", "eth0", "address", sub.mac)
        sh("ip", "-n", ns, "link", "set", "lo", "up")
        sh("ip", "-n", ns, "link", "set", "eth0", "up")
        sh("ip", "-n", core, "link", "set", port, "master", cfg.access_if)
        # Customers must not talk to each other directly at layer 2.
        sh("ip", "-n", core, "link", "set", port, "type", "bridge_slave", "isolated", "on")
        sh("ip", "-n", core, "link", "set", port, "up")
    etc = Path("/etc/netns") / ns
    etc.mkdir(parents=True, exist_ok=True)
    resolv = etc / "resolv.conf"
    resolv.touch()
    p = subprocess.run(
        ["ip", "netns", "exec", ns, "busybox", "udhcpc", "-i", "eth0", "-n", "-q",
         "-t", "5", "-T", "1", "-s", str(UDHCPC_SCRIPT)],
        capture_output=True, text=True, env={**os.environ, "EYESP_RESOLV": str(resolv)},
    )
    if p.returncode:
        raise Error(f"DHCP failed for {sub.name}: {p.stderr.strip()}")
    addr = sh("ip", "-n", ns, "-4", "-br", "addr", "show", "eth0").stdout.split()
    return addr[2] if len(addr) > 2 else "?"


def disconnect(sub: Subscriber) -> None:
    ns = customer_ns(sub)
    if ns_exists(ns):
        sh("ip", "netns", "del", ns)
    shutil.rmtree(Path("/etc/netns") / ns, ignore_errors=True)


def exec_in(sub: Subscriber, argv: list[str]) -> int:
    ns = customer_ns(sub)
    if not ns_exists(ns):
        raise Error(f"{sub.name} is not connected (run `lab connect {sub.name}`)")
    return subprocess.call(["ip", "netns", "exec", ns, *argv])


def down(cfg: Config) -> None:
    require_tools()
    for name in ("web", "dnsmasq"):
        pidfile = cfg.state_dir / f"{name}.pid"
        try:
            pid = int(pidfile.read_text())
            os.killpg(pid, signal.SIGTERM) if name == "web" else os.kill(pid, signal.SIGTERM)
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            pass
        pidfile.unlink(missing_ok=True)
    for entry in sorted(Path("/run/netns").glob("eyesp-*")) if Path("/run/netns").exists() else []:
        sh("ip", "netns", "del", entry.name, check=False)
        shutil.rmtree(Path("/etc/netns") / entry.name, ignore_errors=True)
    (cfg.state_dir / "dnsmasq.leases").unlink(missing_ok=True)
