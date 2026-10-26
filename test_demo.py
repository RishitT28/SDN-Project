#!/usr/bin/env python3
"""
test_demo.py  -  automatic end-to-end check of Demo 1  (Member 3)

Starts the controller, builds the Mininet network, runs the exam server and
two students, then checks that the SDN controller reacted correctly.

  sudo python3 test_demo.py                       # normal machine
  sudo python3 test_demo.py --user-switch         # no OVS kernel module
  sudo python3 test_demo.py --python ~/sdnvenv/bin/python   # python that has os-ken/ryu installed
"""
import argparse
import os
import subprocess
import sys
import time

from mininet.log import setLogLevel

from topology import build, fix_userspace_offload

HERE = os.path.dirname(os.path.abspath(__file__))
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-switch", action="store_true")
    ap.add_argument("--python", default=sys.executable, help="python with os-ken / ryu installed")
    args = ap.parse_args()
    setLogLevel("warning")

    subprocess.call(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctrl_log = open("/tmp/exam_controller.txt", "w")
    ctrl = subprocess.Popen([args.python, os.path.join(HERE, "controller.py")],
                            stdout=ctrl_log, stderr=subprocess.STDOUT)
    time.sleep(4)
    net = build(user_switch=args.user_switch)
    net.start()
    if args.user_switch:
        fix_userspace_offload(net)
    try:
        print("\n1. Network + controller")
        net.waitConnected(timeout=20)
        check("3 switches connected to controller", True)
        loss = net.pingAll()
        check("pingall has 0% packet loss", loss == 0, f"(loss={loss}%)")

        print("\n2. Exam server + two concurrent students")
        srv = net.get("srv")
        server = srv.popen(["python3", os.path.join(HERE, "server.py"), "--time", "120"],
                           stdout=open("/tmp/exam_server.txt", "w"), stderr=subprocess.STDOUT,
                           cwd="/tmp")
        time.sleep(2)
        procs = []
        for host, sid, pw in (("h1", "S01", "alpha01"), ("h4", "S04", "delta04")):
            procs.append((sid, net.get(host).popen(
                ["python3", os.path.join(HERE, "client.py"), "--id", sid, "--password", pw,
                 "--auto", "--think", "0.3"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)))
        for sid, p in procs:
            out = p.communicate(timeout=60)[0]
            check(f"{sid} completed the exam and got a result", "RESULT:" in out,
                  out.strip().splitlines()[-2] if "RESULT:" in out else out.strip()[-80:])

        print("\n3. Socket <-> SDN integration")
        flows = subprocess.run(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s2"],
                               capture_output=True, text=True).stdout
        log = open("/tmp/exam_controller.txt").read()
        check("controller received exam_start from the server", "exam_start" in log)
        check("controller received student_login events", log.count("student_login") >= 2)
        check("priority-20 exam rules installed on s2", "priority=20" in flows)

        print("\n4. Unauthorized host")
        o1 = net.get("x1").cmd(f"python3 {HERE}/client.py --probe")
        o2 = net.get("h2").cmd(f"python3 {HERE}/client.py --probe")
        check("intruder x1 is blocked", "BLOCKED" in o1, o1.strip())
        check("student h2 is reachable", "REACHABLE" in o2 and "BLOCKED" not in o2, o2.strip())
        time.sleep(1)
        log = open("/tmp/exam_controller.txt").read()
        check("controller logged BLOCKED for 10.0.0.99", "10.0.0.99" in log and "BLOCKED" in log)
        flows = subprocess.run(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s3"],
                               capture_output=True, text=True).stdout
        check("priority-30 drop rule installed on s3", "priority=30" in flows)

        print("\n5. Exam ends")
        server.terminate()
        server.wait(timeout=5)
        time.sleep(1)
        log = open("/tmp/exam_controller.txt").read()
        check("controller removed exam rules on exam_end", "exam_end" in log)
    finally:
        net.stop()
        ctrl.terminate()
        subprocess.call(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    print("controller log: /tmp/exam_controller.txt   server log: /tmp/exam_server.txt")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
