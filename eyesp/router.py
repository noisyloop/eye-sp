"""Push configuration to the router and pull usage counters back."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess

from . import render
from .config import Config
from .db import DB, Error, current_period


def _ns(cfg: Config, argv: list[str]) -> list[str]:
    return ["ip", "netns", "exec", cfg.netns, *argv] if cfg.netns else argv


def run(cfg: Config, argv: list[str], stdin: str | None = None, check: bool = True) -> str:
    p = subprocess.run(_ns(cfg, argv), input=stdin, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise Error(f"{' '.join(argv)} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def router_up(cfg: Config) -> bool:
    if not cfg.netns:
        return True
    return os.path.exists(f"/run/netns/{cfg.netns}")


COUNTER_RE = re.compile(r"^sub(\d+)_(up|down)$")


def harvest_usage(cfg: Config, db: DB) -> None:
    """Read-and-zero the per-subscriber nft counters into the database."""
    p = subprocess.run(
        _ns(cfg, ["nft", "-j", "reset", "counters", "table", "ip", render.TABLE]),
        capture_output=True, text=True,
    )
    if p.returncode != 0:  # table not created yet
        return
    totals: dict[int, list[int]] = {}
    for item in json.loads(p.stdout or "{}").get("nftables", []):
        c = item.get("counter")
        m = c and COUNTER_RE.match(c.get("name", ""))
        if not m:
            continue
        t = totals.setdefault(int(m[1]), [0, 0])
        t[0 if m[2] == "up" else 1] += c.get("bytes", 0)
    known = {s.id for s in db.subscribers()}
    period = current_period()
    for sub_id, (up, down) in totals.items():
        if sub_id in known and (up or down):
            db.add_usage(sub_id, period, up, down)


def leaf_qdisc(cfg: Config) -> str | None:
    """Use fq_codel when the kernel has it."""
    if subprocess.run(_ns(cfg, ["tc", "qdisc", "add", "dev", "lo", "root", "fq_codel"]),
                      capture_output=True).returncode:
        return None
    subprocess.run(_ns(cfg, ["tc", "qdisc", "del", "dev", "lo", "root"]), capture_output=True)
    return "fq_codel"


def apply(cfg: Config, db: DB) -> None:
    subs = db.subscribers()
    plans = {p.name: p for p in db.plans()}

    # Save the counters before the new ruleset resets them.
    harvest_usage(cfg, db)

    cfg.dhcp_hosts_path.write_text(render.dhcp_hosts(subs))
    try:
        os.kill(int(cfg.dnsmasq_pid.read_text()), signal.SIGHUP)  # re-read hosts file
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pass

    run(cfg, ["nft", "-f", "-"], stdin=render.nft_ruleset(cfg, subs))
    # -force: keep going when "qdisc del" finds nothing to delete.
    p = subprocess.run(
        _ns(cfg, ["tc", "-force", "-batch", "-"]),
        input=render.tc_batch(cfg, subs, plans, leaf_qdisc(cfg)), capture_output=True, text=True,
    )
    errors = [l for l in p.stderr.splitlines()
              if l.startswith("Error") and "Cannot delete qdisc with handle of zero" not in l]
    if errors:
        raise Error("tc failed:\n" + "\n".join(errors))
