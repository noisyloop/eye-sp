"""Subscriber, plan and usage storage (SQLite)."""

from __future__ import annotations

import datetime as dt
import ipaddress
import random
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    name                 TEXT PRIMARY KEY,
    down_mbps            INTEGER NOT NULL,
    up_mbps              INTEGER NOT NULL,
    price_cents          INTEGER NOT NULL,
    cap_gb               INTEGER,            -- NULL = unlimited
    overage_cents_per_gb INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS subscribers (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT UNIQUE NOT NULL,
    plan    TEXT NOT NULL REFERENCES plans(name),
    mac     TEXT UNIQUE NOT NULL,
    ip      TEXT UNIQUE NOT NULL,
    status  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    sub_id     INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    period     TEXT NOT NULL,                -- YYYY-MM
    up_bytes   INTEGER NOT NULL DEFAULT 0,
    down_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sub_id, period)
);
"""

DEFAULT_PLANS = [
    # name, down, up, price, cap_gb, overage per GB
    ("starter", 10, 2, 2500, None, 0),
    ("metered", 50, 10, 1500, 50, 100),
    ("fiber", 100, 20, 6000, None, 0),
]

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
MAX_SUBSCRIBERS = 0xFFFE  # ids double as tc class ids / packet marks


class Error(Exception):
    """A user-facing error."""


@dataclass
class Plan:
    name: str
    down_mbps: int
    up_mbps: int
    price_cents: int
    cap_gb: int | None
    overage_cents_per_gb: int


@dataclass
class Subscriber:
    id: int
    name: str
    plan: str
    mac: str
    ip: str
    status: str
    created: str


def current_period(today: dt.date | None = None) -> str:
    return (today or dt.date.today()).strftime("%Y-%m")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        if not self.conn.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
            self.conn.executemany("INSERT INTO plans VALUES (?,?,?,?,?,?)", DEFAULT_PLANS)
        self.conn.commit()

    # -- plans ---------------------------------------------------------------
    def plans(self) -> list[Plan]:
        return [Plan(**r) for r in self.conn.execute("SELECT * FROM plans ORDER BY price_cents")]

    def plan(self, name: str) -> Plan:
        row = self.conn.execute("SELECT * FROM plans WHERE name = ?", (name,)).fetchone()
        if not row:
            raise Error(f"no such plan: {name}")
        return Plan(**row)

    def add_plan(self, plan: Plan) -> None:
        if not NAME_RE.match(plan.name):
            raise Error(f"invalid plan name: {plan.name!r}")
        if plan.down_mbps <= 0 or plan.up_mbps <= 0:
            raise Error("speeds must be positive")
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO plans VALUES (?,?,?,?,?,?)",
                    (plan.name, plan.down_mbps, plan.up_mbps, plan.price_cents,
                     plan.cap_gb, plan.overage_cents_per_gb),
                )
        except sqlite3.IntegrityError:
            raise Error(f"plan already exists: {plan.name}") from None

    # -- subscribers ---------------------------------------------------------
    def subscribers(self) -> list[Subscriber]:
        return [Subscriber(**r) for r in self.conn.execute("SELECT * FROM subscribers ORDER BY id")]

    def subscriber(self, name: str) -> Subscriber:
        row = self.conn.execute("SELECT * FROM subscribers WHERE name = ?", (name,)).fetchone()
        if not row:
            raise Error(f"no such subscriber: {name}")
        return Subscriber(**row)

    def add_subscriber(self, name: str, plan: str, pool, mac: str | None = None) -> Subscriber:
        if not NAME_RE.match(name):
            raise Error(f"invalid subscriber name {name!r} (lowercase letters, digits, dashes)")
        self.plan(plan)
        mac = (mac or random_mac()).lower()
        if not MAC_RE.match(mac):
            raise Error(f"invalid MAC address: {mac}")
        used = {r["ip"] for r in self.conn.execute("SELECT ip FROM subscribers")}
        ip = next((str(a) for a in pool if str(a) not in used), None)
        if ip is None:
            raise Error("address pool exhausted")
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO subscribers (name, plan, mac, ip, created) VALUES (?,?,?,?,?)",
                    (name, plan, mac, ip, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
                )
                if cur.lastrowid > MAX_SUBSCRIBERS:
                    raise Error("subscriber id space exhausted")
        except sqlite3.IntegrityError as e:
            raise Error(f"subscriber name or MAC already in use ({e})") from None
        return self.subscriber(name)

    def set_status(self, name: str, status: str) -> None:
        self.subscriber(name)
        with self.conn:
            self.conn.execute("UPDATE subscribers SET status = ? WHERE name = ?", (status, name))

    def set_plan(self, name: str, plan: str) -> None:
        self.subscriber(name)
        self.plan(plan)
        with self.conn:
            self.conn.execute("UPDATE subscribers SET plan = ? WHERE name = ?", (plan, name))

    def delete_subscriber(self, name: str) -> None:
        self.subscriber(name)
        with self.conn:
            self.conn.execute("DELETE FROM subscribers WHERE name = ?", (name,))

    # -- usage ---------------------------------------------------------------
    def add_usage(self, sub_id: int, period: str, up: int, down: int) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO usage (sub_id, period, up_bytes, down_bytes) VALUES (?,?,?,?)
                   ON CONFLICT (sub_id, period) DO UPDATE SET
                     up_bytes = up_bytes + excluded.up_bytes,
                     down_bytes = down_bytes + excluded.down_bytes""",
                (sub_id, period, up, down),
            )

    def usage(self, period: str) -> dict[int, tuple[int, int]]:
        rows = self.conn.execute(
            "SELECT sub_id, up_bytes, down_bytes FROM usage WHERE period = ?", (period,)
        )
        return {r["sub_id"]: (r["up_bytes"], r["down_bytes"]) for r in rows}


def random_mac() -> str:
    # 02:... = locally administered, unicast.
    return "02:e5:" + ":".join(f"{random.randrange(256):02x}" for _ in range(4))


def valid_ip(ip: str) -> bool:
    try:
        ipaddress.IPv4Address(ip)
        return True
    except ValueError:
        return False
