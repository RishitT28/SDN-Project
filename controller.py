#!/usr/bin/env python3
"""
controller.py  -  SDN controller for the exam network  (Member 2)

Framework: Ryu / os-ken, OpenFlow 1.3.   Run:  python3 controller.py

What it does
  1. LEARNING SWITCH   learns which port each MAC is on, installs forwarding rules.
  2. APP EVENTS        the exam server sends JSON lines on a UNIX socket:
                         exam_start   -> start protecting the exam port
                         student_login-> logged
                         exam_end     -> remove the exam rules again
  3. EXAM PRIORITY     while an exam runs, packets to the exam port are handled by
                       priority-20 rules (higher than normal priority-1 rules).
  4. ROSTER FILTER     a host that is NOT in the roster gets a priority-30 DROP rule.
  5. MONITOR           every 2 s the exam-flow byte counters are read and logged.

Flow-table priorities (highest wins):
     30  drop    exam traffic from unknown hosts
     20  forward exam traffic from roster hosts
      5  send to controller: "check this exam packet" (only while exam is running)
      1  normal learning-switch forwarding
      0  table-miss: send to controller
"""
import json
import logging
import os
import sys
import time

from eventlet.green import socket          # cooperative sockets (must not block Ryu's loop)

try:                                        # Ryu ...
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
    from ryu.lib import hub
    from ryu.lib.packet import packet, ethernet, ipv4, tcp, ether_types
    from ryu.ofproto import ofproto_v1_3
except ImportError:                         # ... or os-ken (maintained fork, same API)
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
    from os_ken.lib import hub
    from os_ken.lib.packet import packet, ethernet, ipv4, tcp, ether_types
    from os_ken.ofproto import ofproto_v1_3

BaseApp = getattr(app_manager, "RyuApp", None) or getattr(app_manager, "OSKenApp")

SOCK_PATH = os.environ.get("EXAM_SDN_SOCK", "/tmp/exam_sdn.sock")
PRIO_MISS, PRIO_NORMAL, PRIO_CHECK, PRIO_EXAM, PRIO_BLOCK = 0, 1, 5, 20, 30
COOKIE_EXAM, COOKIE_BLOCK, COOKIE_CHECK = 0x20, 0x30, 0x05     # tags to find/delete rules later
MONITOR_SECONDS = 2


class ExamController(BaseApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}          # dpid -> switch connection
        self.mac_to_port = {}        # dpid -> {mac: port}
        self.exam_active = False
        self.exam_port = 9000
        self.allowed = set()         # roster IPs
        self.blocked = set()         # IPs we have blocked
        self.last_bytes = 0
        hub.spawn(self._northbound_server)
        hub.spawn(self._monitor_loop)

    def log(self, event, detail=""):
        self.logger.info("%-14s %s", event, detail)

    # ===================================================== 1. switch connects
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_connect(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self.mac_to_port.setdefault(dp.id, {})
        # table-miss rule: anything we have no rule for goes to the controller
        parser, ofp = dp.ofproto_parser, dp.ofproto
        self.add_flow(dp, PRIO_MISS, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        self.log("switch_up", f"s{dp.id}")
        if self.exam_active:                       # switch joined mid-exam
            self.install_check_rule(dp)

    @set_ev_cls(ofp_event.EventOFPStateChange, DEAD_DISPATCHER)
    def on_switch_gone(self, ev):
        self.datapaths.pop(ev.datapath.id, None)

    # ========================================================= flow helpers
    def add_flow(self, dp, priority, match, actions, cookie=0, idle=0):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]   # empty = drop
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, cookie=cookie, idle_timeout=idle))

    def delete_by_cookie(self, dp, cookie):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(parser.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      cookie=cookie, cookie_mask=0xFFFFFFFFFFFFFFFF,
                                      match=parser.OFPMatch()))

    def install_check_rule(self, dp):
        """Priority 5: every TCP packet to the exam port is sent to the controller to be checked."""
        parser, ofp = dp.ofproto_parser, dp.ofproto
        match = parser.OFPMatch(eth_type=0x0800, ip_proto=6, tcp_dst=self.exam_port)
        self.add_flow(dp, PRIO_CHECK, match,
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)],
                      cookie=COOKIE_CHECK)

    # ============================================ 2. packets sent to us (packet-in)
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp, parser, ofp = msg.datapath, msg.datapath.ofproto_parser, msg.datapath.ofproto
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return

        # learn: "this source MAC lives behind in_port"
        table = self.mac_to_port[dp.id]
        table[eth.src] = in_port
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)

        ip, tp = pkt.get_protocol(ipv4.ipv4), pkt.get_protocol(tcp.tcp)
        if self.exam_active and ip and tp and tp.dst_port == self.exam_port:
            if ip.src in self.allowed:                       # student on the roster
                if out_port != ofp.OFPP_FLOOD:
                    match = parser.OFPMatch(eth_type=0x0800, ipv4_src=ip.src, ipv4_dst=ip.dst,
                                            ip_proto=6, tcp_dst=self.exam_port)
                    self.add_flow(dp, PRIO_EXAM, match, [parser.OFPActionOutput(out_port)],
                                  cookie=COOKIE_EXAM, idle=30)
                    self.log("exam_flow", f"s{dp.id}: {ip.src} -> {ip.dst}:{self.exam_port} out={out_port}")
            else:                                            # stranger: DROP + remember
                match = parser.OFPMatch(eth_type=0x0800, ipv4_src=ip.src, ip_proto=6,
                                        tcp_dst=self.exam_port)
                self.add_flow(dp, PRIO_BLOCK, match, [], cookie=COOKIE_BLOCK, idle=60)
                if ip.src not in self.blocked:
                    self.blocked.add(ip.src)
                    self.log("BLOCKED", f"{ip.src} is not on the roster (drop rule on s{dp.id})")
                return                                       # do not forward this packet
        elif out_port != ofp.OFPP_FLOOD:                     # normal traffic: install a rule
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst, eth_src=eth.src)
            self.add_flow(dp, PRIO_NORMAL, match, [parser.OFPActionOutput(out_port)], idle=60)

        # forward the packet that triggered all this
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port,
                                        actions=[parser.OFPActionOutput(out_port)], data=msg.data))

    # ============================================ 3. events from the exam server
    def handle_app_event(self, e):
        name = e.get("event")
        if name == "exam_start":
            self.exam_active = True
            self.exam_port = int(e.get("port", 9000))
            self.allowed = set(e.get("allowed", []))
            self.blocked.clear()
            for dp in self.datapaths.values():
                self.install_check_rule(dp)
            self.log("exam_start", f"port={self.exam_port} roster={sorted(self.allowed)}")
        elif name == "student_login":
            self.log("student_login", f"{e.get('student')} from {e.get('ip')}")
        elif name == "student_done":
            self.log("student_done", e.get("student", ""))
        elif name == "exam_end":
            self.exam_active = False
            for dp in self.datapaths.values():
                for ck in (COOKIE_CHECK, COOKIE_EXAM, COOKIE_BLOCK):
                    self.delete_by_cookie(dp, ck)
            self.log("exam_end", "exam rules removed")
        else:
            self.log("unknown_event", str(e))

    def _northbound_server(self):
        """Tiny UNIX-socket server: one JSON object per line."""
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o777)
        srv.listen(16)
        self.log("northbound", f"listening on {SOCK_PATH}")
        while True:
            conn, _ = srv.accept()
            hub.spawn(self._read_events, conn)

    def _read_events(self, conn):
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        self.handle_app_event(json.loads(line))
                    except ValueError:
                        self.log("bad_event", line[:80].decode(errors="replace"))
        finally:
            conn.close()

    # ============================================================ 4. monitoring
    def _monitor_loop(self):
        while True:
            hub.sleep(MONITOR_SECONDS)
            if self.exam_active:
                for dp in list(self.datapaths.values()):
                    parser, ofp = dp.ofproto_parser, dp.ofproto
                    dp.send_msg(parser.OFPFlowStatsRequest(
                        dp, cookie=COOKIE_EXAM, cookie_mask=0xFFFFFFFFFFFFFFFF))

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def on_flow_stats(self, ev):
        total = sum(s.byte_count for s in ev.msg.body)
        if total:
            self.log("monitor", f"s{ev.msg.datapath.id}: exam flows carried {total} bytes "
                                f"in {len(ev.msg.body)} flow(s)")


# ---------------------------------------------------------------- launcher
# `python3 controller.py` works with both Ryu and os-ken (os-ken has no ryu-manager equivalent).
if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    try:
        from ryu.lib import hub as _hub
        from ryu import cfg
        from ryu.base.app_manager import AppManager
        from ryu.controller import controller as _c            # noqa: F401  (registers --ofp-* options)
    except ImportError:
        from os_ken.lib import hub as _hub
        from os_ken import cfg
        from os_ken.base.app_manager import AppManager
        from os_ken.controller import controller as _c         # noqa: F401
    _hub.patch(thread=False)
    cfg.CONF(args=sys.argv[1:], project="exam")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    for noisy in ("os_ken.base.app_manager", "ryu.base.app_manager", "os_ken.controller.controller",
                  "ryu.controller.controller", "ofp_event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    mgr = AppManager.get_instance()
    mgr.load_apps(["controller"])
    services = mgr.instantiate_apps(**mgr.create_contexts())
    try:
        _hub.joinall(services)
    except KeyboardInterrupt:
        pass
    finally:
        mgr.close()
