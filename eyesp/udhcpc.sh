#!/bin/sh
# Minimal busybox udhcpc hook for a customer namespace.
# Only touches the interface, the default route, and $EYESP_RESOLV.
case "$1" in
  deconfig)
    ip -4 addr flush dev "$interface"
    ip link set "$interface" up
    ;;
  bound|renew)
    ip -4 addr flush dev "$interface"
    ip addr add "$ip/$mask" dev "$interface"
    for r in $router; do
      ip route replace default via "$r" dev "$interface"
      break
    done
    if [ -n "$EYESP_RESOLV" ]; then
      : > "$EYESP_RESOLV"
      [ -n "$domain" ] && echo "search $domain" >> "$EYESP_RESOLV"
      for d in $dns; do echo "nameserver $d" >> "$EYESP_RESOLV"; done
    fi
    ;;
esac
exit 0
