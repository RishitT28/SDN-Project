#!/usr/bin/env python3
"""
client.py  -  student client  (Member 1)

  python3 client.py --id S01 --password alpha01            interactive exam
  python3 client.py --id S02 --password bravo02 --auto     simulated student (for tests)
  python3 client.py --probe                                only check if the server is reachable

Every request is timed (send -> full reply line received) and printed as "rtt".
"""
import argparse
import json
import random
import socket
import sys
import time


class Connection:
    """A TCP connection that sends / receives one JSON message per line."""

    def __init__(self, host, port, timeout=5):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""

    def request(self, **msg):
        """Send a message, wait for the reply. Returns (reply, rtt_in_ms)."""
        t0 = time.perf_counter()
        self.sock.sendall((json.dumps(msg) + "\n").encode())
        while b"\n" not in self.buf:                 # TCP is a stream: read until '\n'
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError("server closed the connection")
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line), (time.perf_counter() - t0) * 1000

    def close(self):
        self.sock.close()


def probe(host, port):
    try:
        socket.create_connection((host, port), timeout=4).close()
        print("PROBE: REACHABLE")
        return 0
    except OSError as e:
        print(f"PROBE: BLOCKED/UNREACHABLE ({e})")
        return 1


def login(conn, sid, pw):
    reply, rtt = conn.request(type="LOGIN", id=sid, password=pw)
    if reply["type"] != "LOGIN_OK":
        print("Login failed:", reply.get("reason"))
        return None
    print(f"Logged in as {sid}: {reply['questions']} questions, {reply['time_left']}s left"
          f"{' (session resumed)' if reply['resumed'] else ''}   rtt={rtt:.2f} ms")
    return reply


def show_result(reply):
    print(f"\n=== RESULT: {reply['score']}/{reply['total']}  ({reply['reason']}) ===")


def run_auto(conn, total, think):
    """Answer every question randomly, 'thinking' for about `think` seconds each."""
    rtts = []
    for n in range(total):
        r, rtt = conn.request(type="GET_QUESTION", n=n)
        rtts.append(rtt)
        time.sleep(random.uniform(0.5, 1.5) * think)
        r, rtt = conn.request(type="ANSWER", n=n, choice=random.randrange(4))
        rtts.append(rtt)
    r, rtt = conn.request(type="FINISH")
    show_result(r)
    print(f"requests={len(rtts)}  mean rtt={sum(rtts) / len(rtts):.2f} ms  max={max(rtts):.2f} ms")


def run_interactive(conn, total):
    n = 0
    while n < total:
        r, rtt = conn.request(type="GET_QUESTION", n=n)
        if r["type"] != "QUESTION":
            print(r)
            return
        print(f"\nQ{n + 1}/{total}: {r['text']}   (rtt {rtt:.2f} ms)")
        for i, o in enumerate(r["options"]):
            print(f"   {'ABCD'[i]}) {o}")
        while True:
            c = input("answer (A-D), t = time left, f = finish: ").strip().lower()
            if c == "t":
                r, _ = conn.request(type="TIME_LEFT")
                print("time left:", r.get("seconds"), "s")
            elif c == "f":
                r, _ = conn.request(type="FINISH")
                show_result(r)
                return
            elif c in ("a", "b", "c", "d"):
                r, rtt = conn.request(type="ANSWER", n=n, choice="abcd".index(c))
                print(f"   {r['type']} (rtt {rtt:.2f} ms)")
                if r["type"] == "RESULT":        # timer expired meanwhile
                    show_result(r)
                    return
                n += 1
                break
    r, _ = conn.request(type="FINISH")
    show_result(r)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="10.0.0.100")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--id")
    p.add_argument("--password")
    p.add_argument("--auto", action="store_true", help="simulated student")
    p.add_argument("--think", type=float, default=1.0, help="mean seconds per question (--auto)")
    p.add_argument("--probe", action="store_true", help="only test reachability")
    a = p.parse_args()

    if a.probe:
        sys.exit(probe(a.host, a.port))
    if not (a.id and a.password):
        p.error("--id and --password are required")
    try:
        conn = Connection(a.host, a.port)
        info = login(conn, a.id, a.password)
        if info:
            (run_auto(conn, info["questions"], a.think) if a.auto
             else run_interactive(conn, info["questions"]))
        conn.close()
    except (OSError, ConnectionError) as e:
        print("Connection problem:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
