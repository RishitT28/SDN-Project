# SDN-Controlled Online Examination Network

A multi-user online examination system built on **low-level TCP sockets** and deployed on a **Mininet** network managed by a
**Ryu / os-ken SDN controller**. The exam server reports application events (exam started, student logged in, exam ended) to the
controller, and the controller turns them into OpenFlow rules: it prioritises exam traffic, drops traffic from hosts that are not
on the exam roster, and monitors exam flows.

> Course project: *SDN + Socket Programming (Jackfruit Mini Project), Online Examination Network.*

---

## Features

**Exam application (TCP sockets)**
- Concurrent TCP server, one thread per student, built directly on Python's `socket` module (no web framework)
- Newline-delimited JSON protocol with login, question delivery, answer submission and result messages
- Server-side exam timer with automatic submission on timeout
- Automatic grading, results saved to `results.csv`
- Session resume: a student who reconnects keeps their answers
- Input validation and error replies for malformed messages
- Client with interactive mode, simulated-student mode (`--auto`) and reachability probe (`--probe`); every request's round-trip time is measured

**SDN network**
- Mininet topology with three OpenFlow 1.3 switches, an exam server, six student hosts, a background-traffic host and an unregistered host
- Learning-switch forwarding installed as flow rules
- Exam-aware rules: traffic to the exam port is checked against the roster while an exam is running
- Roster filtering: hosts that are not on the roster get a drop rule at their edge switch
- Flow-statistics monitoring of exam traffic
- Application-to-controller channel over a UNIX socket

---

## Architecture

```
   h1  h2  h3  n1             h4  h5  h6  x1
     \  |  /                    \  |  /
        s2                         s3              Controller (controller.py)
          \                       /                   |
           \                     /                    |  OpenFlow 1.3 (TCP 6653) to s1, s2, s3
                  s1 ------- srv                      |
                       10.0.0.100:9000                |  UNIX socket /tmp/exam_sdn.sock
                                                      +- JSON events from server.py
                                                         exam_start, student_login,
                                                         student_done, exam_end
```

| Host | Address | Role |
|---|---|---|
| `srv` | 10.0.0.100 | Exam server (TCP port 9000) |
| `h1` to `h6` | 10.0.0.1 to 10.0.0.6 | Students (on the roster) |
| `n1` | 10.0.0.11 | Background-traffic host |
| `x1` | 10.0.0.99 | Unregistered host (not on the roster) |

### How it works

1. Switches connect to the controller, which installs a table-miss rule (packets with no matching rule go to the controller).
2. For ordinary traffic the controller learns which port each MAC address is on and installs forwarding rules.
3. When `server.py` starts it sends `exam_start` with the roster of allowed IP addresses. The controller installs a rule on every switch that sends TCP packets for the exam port to the controller for checking.
4. When a host opens an exam connection, the first packet is checked:
   - **On the roster:** the controller installs a forwarding rule for that flow.
   - **Not on the roster:** the controller installs a drop rule and logs the event.
5. The controller polls flow counters every 2 seconds and logs the exam traffic volume.
6. When the server stops it sends `exam_end` and the controller removes all exam rules.

### Flow-table priorities

| Priority | Match | Action |
|---|---|---|
| 30 | Exam-port TCP from a host not on the roster | Drop |
| 20 | Exam-port TCP from a roster host to the server | Forward |
| 5 | Any exam-port TCP packet (installed only while an exam runs) | Send to controller |
| 1 | Source MAC, destination MAC, input port | Forward (learning switch) |
| 0 | Table-miss | Send to controller |

### Application protocol

Each message is one JSON object terminated by `\n`.

| Direction | Message | Fields |
|---|---|---|
| Client to server | `LOGIN` | `id`, `password` |
| | `GET_QUESTION` | `n` |
| | `ANSWER` | `n`, `choice` (0 to 3) |
| | `TIME_LEFT` | |
| | `FINISH` | |
| Server to client | `LOGIN_OK` | `questions`, `time_left`, `resumed` |
| | `LOGIN_FAIL` | `reason` |
| | `QUESTION` | `n`, `text`, `options` |
| | `ACK` | `n` |
| | `TIME_LEFT` | `seconds` |
| | `RESULT` | `score`, `total`, `reason` (`submitted` or `timeout`) |
| | `ERROR` | `reason` |

Events from the server to the controller use the same framing: `{"event": "exam_start", "port": 9000, "allowed": [...]}`.

---

## Repository structure

```
.
├── server.py       TCP exam server
├── client.py       Student client (interactive, --auto, --probe)
├── controller.py   SDN controller (Ryu / os-ken, OpenFlow 1.3)
├── topology.py     Mininet topology
├── test_demo.py    Automated end-to-end test
└── README.md
```

---

## Requirements

- Ubuntu 24.04 or 26.04 (native or virtual machine)
- Python 3.8 or newer
- Mininet, Open vSwitch
- [os-ken](https://pypi.org/project/os-ken/) (a maintained fork of Ryu with the same API). `controller.py` also runs on Ryu if it is installed.

## Installation

```bash
# System packages
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf python3-venv python3-pip
sudo systemctl enable --now openvswitch-switch

# Controller framework in a virtual environment
python3 -m venv ~/sdnvenv
~/sdnvenv/bin/pip install os-ken

# Clone the project
git clone <your-repository-url>
cd <repository-folder>
```

Verify the installation:

```bash
sudo mn --test pingall                                     # ends with "0% dropped"
~/sdnvenv/bin/python -c "import os_ken; print('ok')"       # prints "ok"
```

---

## Quick start (automatic)

One command starts the controller, builds the Mininet network, runs the exam server and two students, checks the SDN behaviour and cleans up. Nothing needs to be typed.

```bash
sudo python3 test_demo.py --python ~/sdnvenv/bin/python
```

Expected result: `12/12 checks passed`. See [Testing](#testing-automatic) for what is checked.

To run the system yourself and interact with it, follow [Usage](#usage-manual-demo) below.

---

## Usage (manual demo)

Start the components yourself and type commands at the `mininet>` prompt. Use this to explore the system or to give a live demonstration. Run each command from the project folder, in three terminals.

### 1. Start the controller (terminal 1)

```bash
sudo fuser -k 6653/tcp 2>/dev/null     # stop any old controller
~/sdnvenv/bin/python controller.py
```

### 2. Start the network (terminal 2)

```bash
sudo mn -c
sudo -E python3 topology.py
```

At the `mininet>` prompt:

```
pingall
srv python3 server.py > /tmp/server.txt 2>&1 &
```

Leave this terminal open. Start each student in its own new terminal, inside the project folder. `mnexec -a` runs the client inside that student's network namespace, so it behaves exactly like the host `h1` or `h4` (no GUI needed):

```bash
# student S01 on host h1
sudo mnexec -a $(pgrep -f "mininet:h1$") python3 client.py --id S01 --password alpha01

# student S04 on host h4 (another terminal)
sudo mnexec -a $(pgrep -f "mininet:h4$") python3 client.py --id S04 --password delta04
```

Answer with `A` to `D`, `t` for time left, `f` to finish and see the score. To run a simulated student from the `mininet>` prompt instead:

```
h1 python3 client.py --id S01 --password alpha01 --auto &
```

### 3. Inspect the network (terminal 3)

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s2 | grep -E "priority=(5|20|30)"
```

Example output while an exam is running:

```
cookie=0x20, ... priority=20,tcp,nw_src=10.0.0.1,nw_dst=10.0.0.100,tp_dst=9000 actions=output:1
cookie=0x5,  ... priority=5,tcp,tp_dst=9000 actions=CONTROLLER:65535
```

### 4. Test the roster filter

```
mininet> x1 python3 client.py --probe      # not on the roster: BLOCKED/UNREACHABLE
mininet> h2 python3 client.py --probe      # on the roster:     REACHABLE
```

The controller logs `BLOCKED 10.0.0.99 is not on the roster`, and a priority-30 drop rule appears on `s3`.

### Client options

| Option | Description |
|---|---|
| `--id`, `--password` | Student credentials |
| `--host`, `--port` | Server address (default `10.0.0.100:9000`) |
| `--auto` | Simulated student that answers randomly |
| `--think` | Mean seconds per question in `--auto` mode (default 1.0) |
| `--probe` | Only test whether the exam port is reachable |

### Server options

| Option | Description |
|---|---|
| `--port` | Listening port (default 9000) |
| `--time` | Exam length per student in seconds (default 300) |
| `--sock` | Controller UNIX socket (default `/tmp/exam_sdn.sock`) |

Demo credentials: `S01/alpha01`, `S02/bravo02`, `S03/charl03`, `S04/delta04`, `S05/echo05`, `S06/foxtr06`. Exam questions and credentials are defined at the top of `server.py`.

---

## Testing (automatic)

`test_demo.py` builds the network, starts the controller and server, runs two concurrent students and verifies the results, without any manual steps. Stop any running controller and network first (`sudo fuser -k 6653/tcp`, `sudo mn -c`):

```bash
sudo python3 test_demo.py --python ~/sdnvenv/bin/python
```

| Check | Verifies |
|---|---|
| Switches connect, `pingall` has 0% loss | Topology and controller connectivity |
| Two concurrent students complete the exam | TCP server concurrency and grading |
| Controller receives `exam_start` and `student_login` | Application to SDN integration |
| Priority-20 rules appear on `s2` | Exam rules installed by the controller |
| Unregistered host is blocked, roster host is reachable | Roster filtering |
| Priority-30 drop rule appears on `s3` | Blocking rule installed |
| Exam rules removed on `exam_end` | Cleanup |

On a machine without the Open vSwitch kernel module (for example WSL), install `ethtool` and add `--user-switch` to `topology.py` and `test_demo.py`.

Logs from the test run are written to `/tmp/exam_controller.txt` and `/tmp/exam_server.txt`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| No `switch_up` lines in the controller output | Another controller holds port 6653: `sudo fuser -k 6653/tcp`, restart the controller, then `sudo mn -c` and restart the network |
| `Error creating interface pair` | `sudo mn -c` |
| `mnexec` prints an error or the client cannot connect | Check that the network is running (`mininet>` prompt open) and that the host name matches, e.g. `pgrep -f "mininet:h1$"` prints a number |
| `controller not reachable` in the server output | Start the controller before the server (the exam still runs without it) |
| `ModuleNotFoundError: mininet` | Run `topology.py` with the system Python (`sudo python3`), not from the virtual environment |

---

## Limitations and future work

- Exam rules are matched on student-to-server traffic only; replies use the normal forwarding rules.
- Exam traffic currently has a higher flow-table priority but is not placed in a dedicated QoS queue.
- The topology is loop-free; redundant links, link-failure rerouting and congestion-based throttling are natural extensions.
- A baseline controller mode and performance measurements (latency, fairness) for comparison are not included in this version.

## License

Add a license file before publishing (for example MIT).
.
