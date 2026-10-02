#!/bin/sh
# Usage: sudo sh restrict-ha-ports.sh <this-node-IP> <peer-IP> [additional-peers...]
set -eu
node_ip=$1
shift
iptables -N CODEX_HA 2>/dev/null || true
iptables -F CODEX_HA
iptables -A CODEX_HA -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A CODEX_HA -i docker0 -j RETURN
iptables -A CODEX_HA -i 'br+' -j RETURN
iptables -A CODEX_HA -s "$node_ip" -j RETURN
for peer_ip in "$@"; do
    iptables -A CODEX_HA -s "$peer_ip" -j RETURN
done
# Match container destination ports, including dynamically published host ports.
iptables -A CODEX_HA -p tcp -m conntrack --ctorigdst "$node_ip" -m multiport --dports 4500,4600 -j DROP
iptables -A CODEX_HA -j RETURN
iptables -C DOCKER-USER -j CODEX_HA 2>/dev/null || iptables -I DOCKER-USER 1 -j CODEX_HA
# The DB Proxy is consumed only by this host and its Docker containers.
iptables -N CODEX_DB 2>/dev/null || true
iptables -F CODEX_DB
iptables -A CODEX_DB -i lo -j ACCEPT
iptables -A CODEX_DB -i docker0 -j ACCEPT
iptables -A CODEX_DB -i 'br+' -j ACCEPT
iptables -A CODEX_DB -s "$node_ip" -j ACCEPT
iptables -A CODEX_DB -j DROP
iptables -C INPUT -d "$node_ip" -p tcp --dport 5000 -j CODEX_DB 2>/dev/null || iptables -I INPUT 1 -d "$node_ip" -p tcp --dport 5000 -j CODEX_DB
