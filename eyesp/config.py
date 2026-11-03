"""Static network settings for the ISP.

Everything here can be overridden with environment variables so the same
control plane can drive the namespace lab or a real Linux router.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # Where the subscriber database and generated configs live.
    state_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("EYESP_STATE", "state")).resolve()
    )
    # Network namespace holding the ISP core router. Empty string = host itself.
    netns: str = field(default_factory=lambda: os.environ.get("EYESP_NETNS", "eyesp-core"))
    wan_if: str = field(default_factory=lambda: os.environ.get("EYESP_WAN_IF", "wan0"))
    access_if: str = field(default_factory=lambda: os.environ.get("EYESP_ACCESS_IF", "br-access"))
    # Customer-facing network. 100.64.0.0/10 is RFC 6598 carrier-grade NAT space.
    access_net: ipaddress.IPv4Network = field(
        default_factory=lambda: ipaddress.ip_network(
            os.environ.get("EYESP_ACCESS_NET", "100.64.0.0/22")
        )
    )
    domain: str = "customers.eyesp.test"

    @property
    def gateway(self) -> ipaddress.IPv4Address:
        return self.access_net.network_address + 1

    @property
    def pool(self) -> list[ipaddress.IPv4Address]:
        # .2-.9 are reserved for ISP infrastructure.
        hosts = list(self.access_net.hosts())
        return hosts[9:]

    @property
    def db_path(self) -> Path:
        return self.state_dir / "eyesp.db"

    @property
    def dhcp_hosts_path(self) -> Path:
        return self.state_dir / "dhcp-hosts"

    @property
    def dnsmasq_pid(self) -> Path:
        return self.state_dir / "dnsmasq.pid"
