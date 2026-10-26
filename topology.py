#!/usr/bin/env python3
"""
topology.py  -  Mininet network  (Member 3)

                    srv (10.0.0.100)
                     |
                    s1            <- core switch
                  /    \
                s2      s3
        h1 h2 h3 n1    h4 h5 h6 x1

  h1..h6  students            10.0.0.1  - 10.0.0.6
  n1      background traffic  10.0.0.11   (used in demo 2: congestion)
  x1      intruder            10.0.0.99   (NOT on the roster)
  srv     exam server         10.0.0.100

Run (controller must already be running):   sudo python3 topology.py
Add --user-switch if Open vSwitch has no kernel module (WSL, some VMs).
"""
import sys

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from functools import partial

from mininet.node import OVSSwitch, RemoteController


def build(user_switch=False):
    # --user-switch = Open vSwitch userspace datapath, for machines without the kernel module
    switch = partial(OVSSwitch, datapath="user") if user_switch else OVSSwitch
    net = Mininet(controller=None, switch=switch, link=TCLink, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)

    s1, s2, s3 = [net.addSwitch(n, protocols="OpenFlow13") for n in ("s1", "s2", "s3")]
    srv = net.addHost("srv", ip="10.0.0.100/24")
    students = [net.addHost(f"h{i}", ip=f"10.0.0.{i}/24") for i in range(1, 7)]
    n1 = net.addHost("n1", ip="10.0.0.11/24")
    x1 = net.addHost("x1", ip="10.0.0.99/24")

    net.addLink(srv, s1)
    net.addLink(s1, s2, bw=10)               # 10 Mbit/s core links (so congestion is possible later)
    net.addLink(s1, s3, bw=10)
    for h in students[:3] + [n1]:
        net.addLink(h, s2)
    for h in students[3:] + [x1]:
        net.addLink(h, s3)
    return net


def fix_userspace_offload(net):
    """Userspace switches need checksum offload off on every host interface (else TCP hangs)."""
    for host in net.hosts:
        for intf in host.intfList():
            host.cmd(f"ethtool -K {intf} tx off rx off")


if __name__ == "__main__":
    setLogLevel("info")
    user_switch = "--user-switch" in sys.argv
    net = build(user_switch)
    net.start()
    if user_switch:
        fix_userspace_offload(net)
    print("\nNetwork is up.  Try:  pingall\n")
    CLI(net)
    net.stop()
