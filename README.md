# eye-sp

A tiny ISP in a box. One Linux machine, a few hundred lines of Python, and the
same building blocks real ISPs use:

| ISP job                     | How eye-sp does it                                              |
|-----------------------------|-----------------------------------------------------------------|
| Hand out addresses          | `dnsmasq` DHCP with one static lease per subscriber (unknown devices get nothing) |
| Resolve names               | `dnsmasq` caching DNS on the gateway                            |
| Share one public IP         | `nftables` NAT (subscribers live in 100.64.0.0/22, RFC 6598 CGNAT space) |
| Sell speed tiers            | `tc` HTB shaping, both directions, one class per subscriber     |
| Stop spoofing               | upstream traffic must match the subscriber's MAC *and* IP       |
| Keep customers apart        | bridge port isolation, so customers can't reach each other directly |
| Cut off non-payers          | `sub suspend` drops their traffic, including open connections   |
| Meter usage                 | per-subscriber `nftables` byte counters, rolled up per month    |
| Send the bill               | plans with price, optional data cap and per-GB overage          |

The subscriber database is SQLite; everything else is rendered from it.

## Try it: an ISP, the internet and customers on your laptop

`eyesp lab` builds a whole ISP out of network namespaces, with no VMs and no containers:

```
 [eyesp-c1] alice ---+
 [eyesp-c2] bob   ---+-- br-access 100.64.0.1/22
                         [eyesp-core]  DHCP · DNS · NAT · shaping · metering
                         wan0 203.0.113.2/30
                           |
                         [eyesp-inet]  "the internet" (web.eyesp.test = 198.51.100.10)
```

Requirements: Linux, root, Python 3.10+, and
`apt install iproute2 nftables dnsmasq busybox curl`.

```console
$ sudo python3 -m eyesp lab up
lab is up: internet <-> eyesp-core <-> customers

$ sudo python3 -m eyesp plan list
PLAN     SPEED (down/up)  PRICE/MO  DATA       OVERAGE
metered  50/10 Mbps       $15.00    50 GB      $1.00/GB
starter  10/2 Mbps        $25.00    unlimited  -
fiber    100/20 Mbps      $60.00    unlimited  -

$ sudo python3 -m eyesp sub add alice --plan starter
added alice: 100.64.0.10 (02:e5:c2:35:5f:e8) on starter
router updated
$ sudo python3 -m eyesp sub add bob --plan fiber

$ sudo python3 -m eyesp lab connect          # plug the customers in; they DHCP
alice: got 100.64.0.10/22 via DHCP
bob: got 100.64.0.11/22 via DHCP

$ sudo python3 -m eyesp lab exec alice -- curl http://web.eyesp.test/
<h1>Hello from the internet!</h1>

$ sudo python3 -m eyesp lab exec alice -- curl -o /dev/null http://web.eyesp.test/speedtest.bin
#  ~9.6 Mbit/s for alice (starter), ~95 Mbit/s for bob (fiber)

$ sudo python3 -m eyesp sub suspend alice    # alice can't reach the internet now
$ sudo python3 -m eyesp sub resume alice

$ sudo python3 -m eyesp usage
usage for 2026-11
NAME   DOWNLOADED  UPLOADED  TOTAL
alice  4.2 MB      1.5 MB    5.7 MB
bob    4.2 MB      6.5 KB    4.2 MB

$ sudo python3 -m eyesp bill
invoices for 2026-11
NAME   PLAN     USED    OVERAGE  AMOUNT
alice  starter  5.7 MB  -        $25.00
bob    fiber    4.2 MB  -        $60.00

monthly revenue: $85.00

$ sudo python3 -m eyesp lab down
```

## Commands

```
eyesp plan list | add NAME --down MBPS --up MBPS --price USD [--cap-gb N --overage USD]
eyesp sub  list | add NAME [--plan P] [--mac MAC] | suspend | resume | delete NAME | plan NAME PLAN
eyesp apply                       # push config to the router (sub commands do this automatically)
eyesp show nft|tc|dhcp|dnsmasq    # print the generated config without touching anything
eyesp usage [--period YYYY-MM]
eyesp bill  [--period YYYY-MM]
eyesp lab   up | down | connect [NAME...] | exec NAME -- CMD...
```

State lives in `./state` (override with `EYESP_STATE`).

## How it works

* `eyesp/db.py`: plans, subscribers (each gets an id, a MAC and an IP from the pool) and monthly usage.
* `eyesp/render.py`: pure functions that turn the database into a dnsmasq hosts file, one atomic
  nftables ruleset, and a `tc` batch. Try `eyesp show nft`.
* `eyesp/router.py`: applies that config. Before replacing the ruleset it reads and zeroes the
  byte counters into the database, so no usage is lost on a reconfigure.
* `eyesp/lab.py`: the namespace playground.

Speed tiers don't use tc filters. The nftables forward chain sets each packet's `skb->priority` to the
subscriber's HTB class (`meta priority set 1:<id>`), and HTB classifies on that directly. Upload is
shaped leaving `wan0` and download leaving `br-access`. This works even on minimal kernels without
the `cls_fw`/`cls_u32` modules.

## From lab to a real (tiny) ISP

The control plane doesn't care whether it runs in a namespace. On a real Linux router with an
upstream port and a customer-facing bridge (wired, or a WISP's access points):

```sh
export EYESP_NETNS=""            # configure the host itself, not a namespace
export EYESP_WAN_IF=eth0         # upstream / transit
export EYESP_ACCESS_IF=br0       # customer side, with 100.64.0.1/22 on it
python3 -m eyesp show dnsmasq > /etc/dnsmasq.d/eyesp.conf   # uses public resolvers upstream
sysctl -w net.ipv4.ip_forward=1
python3 -m eyesp sub add alice --plan fiber --mac <alice's router MAC>
python3 -m eyesp apply
```

Run `eyesp usage` from cron so counters are harvested regularly.

Still missing compared to a grown-up ISP (good next steps): IPv6, PPPoE or RADIUS
authentication, a captive portal for suspended customers, BGP to more than one upstream,
redundancy, and payments.

## Tests

```sh
python3 -m unittest discover -s tests          # unit tests, no privileges needed
sudo python3 -m unittest discover -s tests     # + end-to-end lab test (DHCP, NAT, shaping, isolation, billing)
```
