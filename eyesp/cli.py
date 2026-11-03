"""Command line for running the ISP."""

from __future__ import annotations

import argparse
import sys

from . import lab, render, router
from .config import Config
from .db import DB, Error, Plan, current_period


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


def table(rows: list[list[str]], header: list[str]) -> None:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    for row in [header, *rows]:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())


def maybe_apply(cfg: Config, db: DB, args) -> None:
    if getattr(args, "no_apply", False) or not router.router_up(cfg):
        return
    router.apply(cfg, db)
    print("router updated")


# -- commands ------------------------------------------------------------------

def cmd_plan_list(cfg, db, args):
    table(
        [[p.name, f"{p.down_mbps}/{p.up_mbps} Mbps", money(p.price_cents),
          f"{p.cap_gb} GB" if p.cap_gb is not None else "unlimited",
          money(p.overage_cents_per_gb) + "/GB" if p.cap_gb is not None else "-"]
         for p in db.plans()],
        ["PLAN", "SPEED (down/up)", "PRICE/MO", "DATA", "OVERAGE"],
    )


def cmd_plan_add(cfg, db, args):
    db.add_plan(Plan(args.name, args.down, args.up, round(args.price * 100), args.cap_gb,
                     round(args.overage * 100)))
    print(f"added plan {args.name}")


def cmd_sub_list(cfg, db, args):
    table([[s.id, s.name, s.plan, s.ip, s.mac, s.status] for s in db.subscribers()],
          ["ID", "NAME", "PLAN", "IP", "MAC", "STATUS"])


def cmd_sub_add(cfg, db, args):
    s = db.add_subscriber(args.name, args.plan, cfg.pool, args.mac)
    print(f"added {s.name}: {s.ip} ({s.mac}) on {s.plan}")
    maybe_apply(cfg, db, args)


def cmd_sub_status(status):
    def fn(cfg, db, args):
        db.set_status(args.name, status)
        print(f"{args.name} is now {status}")
        maybe_apply(cfg, db, args)
    return fn


def cmd_sub_plan(cfg, db, args):
    db.set_plan(args.name, args.plan)
    print(f"{args.name} moved to {args.plan}")
    maybe_apply(cfg, db, args)


def cmd_sub_delete(cfg, db, args):
    sub = db.subscriber(args.name)
    if router.router_up(cfg):
        router.harvest_usage(cfg, db)
    if cfg.netns and lab.ns_exists(lab.customer_ns(sub)):
        lab.disconnect(sub)
    db.delete_subscriber(args.name)
    print(f"deleted {args.name}")
    maybe_apply(cfg, db, args)


def cmd_apply(cfg, db, args):
    router.apply(cfg, db)
    print(f"applied config for {len(db.subscribers())} subscribers")


def cmd_show(cfg, db, args):
    subs = db.subscribers()
    if args.what == "nft":
        print(render.nft_ruleset(cfg, subs), end="")
    elif args.what == "tc":
        print(render.tc_batch(cfg, subs, {p.name: p for p in db.plans()}), end="")
    elif args.what == "dhcp":
        print(render.dhcp_hosts(subs), end="")
    elif args.what == "dnsmasq":
        print(render.dnsmasq_conf(cfg), end="")


def cmd_usage(cfg, db, args):
    if router.router_up(cfg):
        router.harvest_usage(cfg, db)
    usage = db.usage(args.period)
    rows = []
    for s in db.subscribers():
        up, down = usage.get(s.id, (0, 0))
        rows.append([s.name, human(down), human(up), human(up + down)])
    print(f"usage for {args.period}")
    table(rows, ["NAME", "DOWNLOADED", "UPLOADED", "TOTAL"])


def cmd_bill(cfg, db, args):
    if router.router_up(cfg):
        router.harvest_usage(cfg, db)
    usage = db.usage(args.period)
    plans = {p.name: p for p in db.plans()}
    lines = [render.invoice(s, plans[s.plan], *usage.get(s.id, (0, 0))) for s in db.subscribers()]
    print(f"invoices for {args.period}")
    table(
        [[l.sub.name, l.plan.name, human(l.used_bytes),
          f"{l.overage_gb} GB" if l.overage_gb else "-", money(l.amount_cents)] for l in lines],
        ["NAME", "PLAN", "USED", "OVERAGE", "AMOUNT"],
    )
    print(f"\nmonthly revenue: {money(sum(l.amount_cents for l in lines))}")


def cmd_lab_up(cfg, db, args):
    lab.up(cfg, db)
    print("lab is up: internet <-> eyesp-core <-> customers")
    print("next: eyesp sub add alice --plan starter && eyesp lab connect alice")


def cmd_lab_down(cfg, db, args):
    lab.down(cfg)
    print("lab is down")


def cmd_lab_connect(cfg, db, args):
    names = args.names or [s.name for s in db.subscribers()]
    for name in names:
        sub = db.subscriber(name)
        print(f"{name}: got {lab.connect(cfg, sub)} via DHCP")


def cmd_lab_exec(cfg, db, args):
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    return lab.exec_in(db.subscriber(args.name), argv or ["sh"])


# -- parser --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="eyesp", description="A tiny ISP in a box.")
    sp = ap.add_subparsers(dest="cmd", required=True)

    plan = sp.add_parser("plan", help="service plans").add_subparsers(dest="sub", required=True)
    plan.add_parser("list").set_defaults(fn=cmd_plan_list)
    p = plan.add_parser("add")
    p.add_argument("name")
    p.add_argument("--down", type=int, required=True, help="download Mbps")
    p.add_argument("--up", type=int, required=True, help="upload Mbps")
    p.add_argument("--price", type=float, required=True, help="monthly price in dollars")
    p.add_argument("--cap-gb", type=int, help="monthly data cap (default unlimited)")
    p.add_argument("--overage", type=float, default=0, help="dollars per GB over the cap")
    p.set_defaults(fn=cmd_plan_add)

    sub = sp.add_parser("sub", help="subscribers").add_subparsers(dest="sub", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_sub_list)
    p = sub.add_parser("add")
    p.add_argument("name")
    p.add_argument("--plan", default="starter")
    p.add_argument("--mac", help="customer router MAC (random if omitted)")
    p.set_defaults(fn=cmd_sub_add)
    for name, fn in (("suspend", cmd_sub_status("suspended")), ("resume", cmd_sub_status("active")),
                     ("delete", cmd_sub_delete)):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.set_defaults(fn=fn)
    p = sub.add_parser("plan", help="change a subscriber's plan")
    p.add_argument("name")
    p.add_argument("plan")
    p.set_defaults(fn=cmd_sub_plan)
    for p in sub.choices.values():
        if p.get_default("fn") is not cmd_sub_list:
            p.add_argument("--no-apply", action="store_true", help="don't push to the router")

    sp.add_parser("apply", help="push config to the router").set_defaults(fn=cmd_apply)
    p = sp.add_parser("show", help="print generated config")
    p.add_argument("what", choices=["nft", "tc", "dhcp", "dnsmasq"])
    p.set_defaults(fn=cmd_show)
    for name, fn in (("usage", cmd_usage), ("bill", cmd_bill)):
        p = sp.add_parser(name)
        p.add_argument("--period", default=current_period(), help="YYYY-MM")
        p.set_defaults(fn=fn)

    lb = sp.add_parser("lab", help="namespace lab").add_subparsers(dest="sub", required=True)
    lb.add_parser("up").set_defaults(fn=cmd_lab_up)
    lb.add_parser("down").set_defaults(fn=cmd_lab_down)
    p = lb.add_parser("connect", help="plug customers in (all if none given)")
    p.add_argument("names", nargs="*")
    p.set_defaults(fn=cmd_lab_connect)
    p = lb.add_parser("exec", help="run a command as a customer")
    p.add_argument("name")
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_lab_exec)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config()
    try:
        db = DB(cfg.db_path)
        return args.fn(cfg, db, args) or 0
    except Error as e:
        print(f"eyesp: {e}", file=sys.stderr)
        return 1
