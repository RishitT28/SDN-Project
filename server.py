#!/usr/bin/env python3
"""
server.py  -  TCP exam server  (Member 1)

* One TCP socket listens on port 9000. Every student gets their own thread.
* Messages are single lines of JSON ("newline-delimited JSON"), e.g.
      {"type": "LOGIN", "id": "S01", "password": "alpha01"}
* The server tells the SDN controller about exam events (exam_start,
  student_login, student_done, exam_end) through a UNIX socket.

Run:  python3 server.py            (inside Mininet:  srv python3 server.py &)
"""
import argparse
import csv
import json
import os
import signal
import socket
import threading
import time

# ----------------------------------------------------------------- exam data
STUDENTS = {  # id -> password
    "S01": "alpha01", "S02": "bravo02", "S03": "charl03",
    "S04": "delta04", "S05": "echo05", "S06": "foxtr06",
}
# IPs of the lab machines allowed to take the exam (sent to the controller)
ALLOWED_HOSTS = ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.6"]

QUESTIONS = [
    {"q": "Which transport protocol gives reliable, ordered delivery?",
     "options": ["UDP", "TCP", "ICMP", "ARP"], "answer": 1},
    {"q": "In SDN, which component makes the forwarding decisions?",
     "options": ["Switch", "Controller", "Host", "Router"], "answer": 1},
    {"q": "Which protocol does a controller use to program switches?",
     "options": ["OpenFlow", "SMTP", "FTP", "DNS"], "answer": 0},
    {"q": "What does a switch do when no flow rule matches a packet?",
     "options": ["Drops it", "Sends it to the controller", "Reboots", "Loops it"], "answer": 1},
    {"q": "Which socket call makes a server wait for a new client?",
     "options": ["bind()", "listen()", "accept()", "close()"], "answer": 2},
]

NORTHBOUND_SOCK = "/tmp/exam_sdn.sock"

# ------------------------------------------------------------------- state
lock = threading.Lock()          # protects sessions + results file
sessions = {}                    # student id -> dict(answers, deadline, done, score)


def notify_controller(event, **data):
    """Send one event line to the SDN controller. Never crashes the exam."""
    msg = json.dumps({"event": event, **data}) + "\n"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(NORTHBOUND_SOCK)
        s.sendall(msg.encode())
        s.close()
        print(f"[sdn] sent {event}", flush=True)
    except OSError:
        print(f"[sdn] controller not reachable, event '{event}' not sent", flush=True)


def grade(sess):
    score = sum(1 for n, c in sess["answers"].items() if QUESTIONS[int(n)]["answer"] == c)
    return score


def finish(sid, sess, reason):
    """Mark the session done, grade it and append to results.csv (once)."""
    with lock:
        if sess["done"]:
            return
        sess["done"] = True
        sess["score"] = grade(sess)
        new = not os.path.exists("results.csv")
        with open("results.csv", "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "student", "score", "total", "reason"])
            w.writerow([time.strftime("%H:%M:%S"), sid, sess["score"], len(QUESTIONS), reason])
    print(f"[exam] {sid} finished: {sess['score']}/{len(QUESTIONS)} ({reason})", flush=True)
    notify_controller("student_done", student=sid)


def send(conn, **msg):
    conn.sendall((json.dumps(msg) + "\n").encode())


# ------------------------------------------------------- one thread / client
def handle_client(conn, addr, exam_seconds):
    ip = addr[0]
    print(f"[conn] {ip}:{addr[1]} connected", flush=True)
    conn.settimeout(1.0)            # wake up every second to check the timer
    buf = b""
    sid, sess = None, None
    try:
        while True:
            # --- timer: auto-submit when time is up, even if the student is idle
            if sess and not sess["done"] and time.time() > sess["deadline"]:
                finish(sid, sess, "timeout")
                send(conn, type="RESULT", score=sess["score"], total=len(QUESTIONS), reason="timeout")
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break                                   # client closed the connection
            buf += data
            while b"\n" in buf:                         # TCP is a byte stream: split on '\n'
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                    kind = msg["type"]
                except (ValueError, KeyError, TypeError):
                    send(conn, type="ERROR", reason="malformed message")
                    continue

                if kind == "LOGIN":
                    s_id, pw = msg.get("id"), msg.get("password")
                    if STUDENTS.get(s_id) != pw:
                        send(conn, type="LOGIN_FAIL", reason="invalid credentials")
                        continue
                    with lock:
                        if s_id not in sessions:        # new session (else: resume the old one)
                            sessions[s_id] = {"answers": {}, "done": False, "score": 0,
                                              "deadline": time.time() + exam_seconds}
                        sid, sess = s_id, sessions[s_id]
                    print(f"[auth] {sid} logged in from {ip}", flush=True)
                    notify_controller("student_login", student=sid, ip=ip)
                    send(conn, type="LOGIN_OK", questions=len(QUESTIONS),
                         time_left=int(sess["deadline"] - time.time()), resumed=bool(sess["answers"]))
                elif sess is None:
                    send(conn, type="ERROR", reason="login first")
                elif kind == "GET_QUESTION":
                    n = msg.get("n", -1)
                    if sess["done"]:
                        send(conn, type="ERROR", reason="exam already submitted")
                    elif not isinstance(n, int) or not 0 <= n < len(QUESTIONS):
                        send(conn, type="ERROR", reason="no such question")
                    else:
                        q = QUESTIONS[n]
                        send(conn, type="QUESTION", n=n, text=q["q"], options=q["options"])
                elif kind == "ANSWER":
                    n, c = msg.get("n"), msg.get("choice")
                    if sess["done"]:
                        send(conn, type="ERROR", reason="exam already submitted")
                    elif n not in range(len(QUESTIONS)) or c not in range(4):
                        send(conn, type="ERROR", reason="bad answer")
                    else:
                        sess["answers"][str(n)] = c
                        send(conn, type="ACK", n=n)
                elif kind == "TIME_LEFT":
                    send(conn, type="TIME_LEFT", seconds=max(0, int(sess["deadline"] - time.time())))
                elif kind == "FINISH":
                    finish(sid, sess, "submitted")
                    send(conn, type="RESULT", score=sess["score"], total=len(QUESTIONS), reason="submitted")
                else:
                    send(conn, type="ERROR", reason="unknown message type")
    except (ConnectionError, OSError):
        pass
    finally:
        conn.close()
        print(f"[conn] {ip} disconnected", flush=True)


def main():
    global NORTHBOUND_SOCK
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--time", type=int, default=300, help="exam length per student (seconds)")
    p.add_argument("--sock", default=NORTHBOUND_SOCK, help="controller UNIX socket")
    args = p.parse_args()
    NORTHBOUND_SOCK = args.sock

    def stop(*_):                      # `kill` behaves like Ctrl+C, so exam_end is always sent
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)        # 1. create TCP socket
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)      #    allow quick restart
    srv.bind((args.host, args.port))                               # 2. attach to IP:port
    srv.listen(50)                                                 # 3. start listening
    print(f"[server] exam open on {args.host}:{args.port} "
          f"({len(QUESTIONS)} questions, {args.time}s each)", flush=True)
    notify_controller("exam_start", port=args.port, allowed=ALLOWED_HOSTS)
    try:
        while True:
            conn, addr = srv.accept()                              # 4. wait for a student
            threading.Thread(target=handle_client, args=(conn, addr, args.time),
                             daemon=True).start()                  # 5. one thread per student
    except KeyboardInterrupt:
        pass
    finally:
        notify_controller("exam_end")
        srv.close()
        print("[server] stopped", flush=True)


if __name__ == "__main__":
    main()
