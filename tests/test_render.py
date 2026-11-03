import tempfile
import unittest
from pathlib import Path

from eyesp import render
from eyesp.config import Config
from eyesp.db import DB, Error


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(state_dir=Path(self.tmp.name), netns="")
        self.db = DB(self.cfg.db_path)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()


class TestDB(Base):
    def test_default_plans(self):
        self.assertEqual({p.name for p in self.db.plans()}, {"starter", "metered", "fiber"})

    def test_addresses_allocated_from_pool_and_reused(self):
        a = self.db.add_subscriber("alice", "starter", self.cfg.pool)
        b = self.db.add_subscriber("bob", "fiber", self.cfg.pool)
        self.assertEqual((a.ip, b.ip), ("100.64.0.10", "100.64.0.11"))
        self.db.delete_subscriber("alice")
        c = self.db.add_subscriber("carol", "starter", self.cfg.pool)
        self.assertEqual(c.ip, "100.64.0.10")

    def test_validation(self):
        with self.assertRaises(Error):
            self.db.add_subscriber("Bad Name", "starter", self.cfg.pool)
        with self.assertRaises(Error):
            self.db.add_subscriber("x", "nope", self.cfg.pool)
        with self.assertRaises(Error):
            self.db.add_subscriber("x", "starter", self.cfg.pool, mac="zz")
        self.db.add_subscriber("x", "starter", self.cfg.pool)
        with self.assertRaises(Error):
            self.db.add_subscriber("x", "starter", self.cfg.pool)

    def test_usage_accumulates(self):
        s = self.db.add_subscriber("alice", "starter", self.cfg.pool)
        self.db.add_usage(s.id, "2026-11", 10, 100)
        self.db.add_usage(s.id, "2026-11", 1, 1)
        self.assertEqual(self.db.usage("2026-11"), {s.id: (11, 101)})
        self.assertEqual(self.db.usage("2026-10"), {})


class TestRender(Base):
    def setUp(self):
        super().setUp()
        self.alice = self.db.add_subscriber("alice", "starter", self.cfg.pool, mac="02:00:00:00:00:01")
        self.bob = self.db.add_subscriber("bob", "fiber", self.cfg.pool, mac="02:00:00:00:00:02")

    def test_dhcp_hosts(self):
        self.assertEqual(
            render.dhcp_hosts(self.db.subscribers()),
            "02:00:00:00:00:01,100.64.0.10,alice\n02:00:00:00:00:02,100.64.0.11,bob\n",
        )

    def test_nft_ruleset(self):
        self.db.set_status("bob", "suspended")
        rs = render.nft_ruleset(self.cfg, self.db.subscribers())
        self.assertTrue(rs.startswith("table ip eyesp\ndelete table ip eyesp\n"))
        self.assertIn("elements = { 100.64.0.11 }", rs)
        self.assertIn('ether saddr 02:00:00:00:00:01 ip saddr 100.64.0.10 '
                      'meta priority set 1:1 counter name "sub1_up" accept', rs)
        self.assertIn('ip daddr 100.64.0.11 ct state established,related '
                      'meta priority set 1:2 counter name "sub2_down" accept', rs)
        self.assertIn('oifname "wan0" ip saddr 100.64.0.0/22 masquerade', rs)

    def test_tc_rates_per_direction(self):
        plans = {p.name: p for p in self.db.plans()}
        tc = render.tc_batch(self.cfg, self.db.subscribers(), plans).splitlines()
        self.assertIn("class add dev wan0 parent 1: classid 1:1 htb rate 2mbit ceil 2mbit", tc)
        self.assertIn("class add dev br-access parent 1: classid 1:2 htb rate 100mbit ceil 100mbit", tc)
        self.assertIn("qdisc add dev wan0 parent 1:1 fq_codel", tc)
        no_leaf = render.tc_batch(self.cfg, self.db.subscribers(), plans, leaf=None)
        self.assertNotIn("fq_codel", no_leaf)

    def test_class_ids_are_hex(self):
        sub = self.db.subscribers()[0]
        sub.id = 26
        self.assertIn("meta priority set 1:1a", render.nft_ruleset(self.cfg, [sub]))

    def test_dnsmasq_lab_hosts(self):
        conf = render.dnsmasq_conf(self.cfg, {"web.eyesp.test": "198.51.100.10"})
        self.assertIn("address=/web.eyesp.test/198.51.100.10", conf)
        self.assertIn("dhcp-range=100.64.0.0,static,255.255.252.0,12h", conf)
        self.assertNotIn("server=", conf)


class TestBilling(Base):
    def test_unlimited_plan(self):
        p = self.db.plan("fiber")
        s = self.db.add_subscriber("a", "fiber", self.cfg.pool)
        self.assertEqual(render.invoice(s, p, 0, 10**12).amount_cents, 6000)

    def test_overage_rounds_up_per_gb(self):
        p = self.db.plan("metered")  # $15, 50 GB, $1/GB over
        s = self.db.add_subscriber("a", "metered", self.cfg.pool)
        self.assertEqual(render.invoice(s, p, 0, 50 * 10**9).amount_cents, 1500)
        line = render.invoice(s, p, 10**9, 50 * 10**9 + 1)
        self.assertEqual((line.overage_gb, line.amount_cents), (2, 1700))


if __name__ == "__main__":
    unittest.main()
