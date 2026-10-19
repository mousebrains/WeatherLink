#!/bin/sh
# One-time helper, run as root on the NAS: ask the LAN's DHCP server for a
# lease for the weather dashboard's virtual MAC address, so the router (UniFi)
# can then pin that MAC to a fixed IPv4 address.
#
#   sudo sh tools/dhcp_probe.sh [MAC] [PARENT_INTERFACE]
#
# It creates a throwaway Docker macvlan network on the parent interface and runs
# BusyBox udhcpc in a container with that MAC. udhcpc only asks for and prints
# the lease (-s /bin/true configures nothing); the container and network are
# removed afterwards. The host's own network configuration is not touched.
set -eu

MAC=${1:-02:57:4c:4c:00:01}    # locally administered; 57:4c:4c = "WLL"
PARENT=${2:-eno1np0}
NET=wll-dhcp-probe

# Docker gives every container an address before DHCP runs. Use TEST-NET-1
# (RFC 5737, 192.0.2.0/24), which no LAN device uses, so nothing can clash.
docker network create --driver macvlan --opt parent="$PARENT" \
    --subnet 192.0.2.0/24 --gateway 192.0.2.1 --ip-range 192.0.2.2/32 "$NET" >/dev/null
trap 'docker network rm "$NET" >/dev/null 2>&1 || true' EXIT

echo "Requesting a DHCP lease for $MAC (hostname 'weather') on $PARENT ..."
docker run --rm --network "name=$NET,mac-address=$MAC" busybox:1.37 \
    udhcpc -i eth0 -f -q -n -B -t 5 -T 3 -s /bin/true -x hostname:weather
echo "Pin that address to $MAC in UniFi, then point weather.mousebrains.com at it."
