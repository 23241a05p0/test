# Complete Guide: Async API Gateway with a Redis Rate Limiter

A beginner-friendly, build-it-from-scratch guide. By the end you'll have an
asynchronous API Gateway that protects a backend with a Redis token-bucket rate
limiter, packaged with Docker, and load-tested with k6 — and you'll actually
understand every piece.

> **Audience:** you and a friend, both new to these tools, starting from an empty
> folder on your own machines. Follow top to bottom.

---

## Table of contents
1. [The big picture — what are we building and why](#1-the-big-picture)
2. [The concepts, explained simply](#2-the-concepts-explained-simply)
3. [Install the tools (Windows / macOS / Linux)](#3-install-the-tools)
4. [Build it file by file](#4-build-it-file-by-file)
5. [Run it](#5-run-it)
6. [Test it & load test it](#6-test-it--load-test-it)
7. [Troubleshooting (the traps we hit)](#7-troubleshooting)
8. [How to explain this project (interview notes)](#8-how-to-explain-this-project)
9. [Ideas to take it further](#9-ideas-to-take-it-further)

---

## 1. The big picture

An **API Gateway** is a single front door that sits in front of your real
services. Every client request hits the gateway first, and the gateway decides
what to do with it — authenticate it, log it, **rate-limit it**, then forward
("proxy") it to the right backend service.

We're building a small but real version of that:

```
   Clients / k6
        |
        v
  +-------------+        ask: "does this client have a token left?"
  |   GATEWAY   | ----------------------------------------------+
  | aiohttp,    |                                               |
  | async       | <---------------------------------------------+
  +-------------+        answer: yes -> forward,  no -> 429      |
        |                                                        v
        | forward allowed requests                          +---------+
        v                                                    |  REDIS  |
  +-------------+                                            | buckets |
  |  UPSTREAM   |   (a tiny backend that just says "ok")     +---------+
  |  aiohttp    |
  +-------------+
```

**Why each part exists:**
- **Gateway** — the thing we're proud of: it's *asynchronous*, so one process
  handles thousands of simultaneous connections.
- **Rate limiter (in Redis)** — stops any one client from flooding us. Protects
  the backend from overload.
- **Upstream** — a stand-in for "your real service." Keeps the demo honest: the
  gateway really forwards traffic, it doesn't just answer itself.
- **Docker** — so the whole thing starts with one command on any machine.
- **k6** — to prove it's fast and that the limiter works, with real numbers.

---

## 2. The concepts, explained simply

### Synchronous vs. asynchronous (the core idea)
Imagine a waiter taking orders.
- **Synchronous (blocking):** the waiter takes your order, walks to the kitchen,
  **stands there waiting** until the food is cooked, brings it back, then serves
  the next table. One slow dish blocks everyone.
- **Asynchronous (non-blocking):** the waiter takes your order, hands it to the
  kitchen, and **immediately** goes to the next table. When a dish is ready, they
  deliver it. One waiter serves many tables because they never stand idle.

A web server spends most of its time *waiting* — for the network, the database,
the upstream service. Async lets one process serve thousands of requests by never
standing idle during those waits. **That's why our gateway can hit high
requests/second on a single process.**

### `asyncio`
Python's built-in library for writing asynchronous code. The keywords:
- `async def` — defines a function that can pause and resume (a "coroutine").
- `await` — "pause here until this slow thing finishes, and let other work run
  meanwhile." You `await` network calls, Redis calls, etc.
- The **event loop** is the engine that juggles all these paused/resumed
  coroutines on a single thread.

### `aiohttp`
An async HTTP library built on `asyncio`. We use it two ways:
- **Server** — to build the gateway and the upstream (handle incoming requests).
- **Client** — inside the gateway, to forward requests to the upstream
  (`ClientSession`).

Using one shared `ClientSession` reuses TCP connections (a "connection pool"),
which is a big speed win versus opening a new connection per request.

### Redis
An extremely fast **in-memory** key-value store (data lives in RAM, so reads and
writes take microseconds). We use it to hold each client's rate-limit counter.

**Why not just keep counters in Python memory?** Because if you run multiple
gateway processes/containers (which you do in production for scale), each would
have its own separate counter and the limit wouldn't be enforced globally. Redis
is **shared state** every gateway instance can read and update. It's the single
source of truth.

### Rate limiting & the Token Bucket algorithm
**Rate limiting** = capping how many requests a client may make in a time window,
so no one can overwhelm your service.

There are several algorithms; we use **Token Bucket** because it's simple,
widely used in industry (AWS, Stripe), and allows short bursts:

- Picture a bucket that holds up to **`BURST`** tokens (its capacity).
- Tokens are **refilled at `RATE` per second**, up to the capacity.
- **Every request must take 1 token.** Token available → request allowed.
  Bucket empty → request rejected with **HTTP 429 Too Many Requests**.

This gives you a nice property: a client can briefly **burst** (spend the full
bucket fast), then they're limited to the steady **`RATE`** as tokens trickle
back. Example with `RATE=100`, `BURST=200`: a client can fire 200 requests
instantly, then is throttled to 100/sec afterward.

**Why run it inside Redis as a Lua script?** Two concurrent requests could both
read "1 token left" and both spend it — a race condition that lets clients exceed
the limit. Redis runs a **Lua script atomically** (nothing else runs in the
middle), so "read tokens, refill, spend, save" happens as one indivisible step.
No races, ever. See [gateway/token_bucket.lua](gateway/token_bucket.lua).

### HTTP 429
The standard status code for "Too Many Requests." Well-behaved clients see a 429
and back off / retry later. It's a *successful* outcome for a rate limiter — it
means the protection is working.

### Docker & Docker Compose
- **Docker** packages an app plus everything it needs (Python version, libraries,
  OS bits) into an **image**. A running image is a **container** — isolated, and
  identical on every machine. "Works on my machine" stops being a problem.
- A **Dockerfile** is the recipe for building an image.
- **Docker Compose** describes a *multi-container* app in one
  `docker-compose.yml` and starts them all with `docker compose up`. Our compose
  file wires together three containers (redis, upstream, gateway) on a private
  network where they find each other by name (e.g. `redis`, `upstream`).

### k6
A modern **load-testing** tool. You write a small JavaScript file describing the
traffic (how many **virtual users (VUs)**, for how long), and k6 hammers your
service and reports:
- **`http_reqs`** — total requests and **requests/second** (throughput).
- **`http_req_duration`** — latency, especially **`p(95)`** (95% of requests were
  faster than this). We assert `p(95) < 70ms`.
- custom metrics — we add `rate_limited` to track the fraction of 429s.

A **threshold** makes k6 automatically PASS/FAIL a run (e.g. fail if p95 ≥ 70ms).

---

## 3. Install the tools

You need three things: **Python 3.12+**, **Docker Desktop**, and **k6**.

### Windows (using winget, built into Windows 11)
Open PowerShell or Git Bash and run:
```bash
winget install Python.Python.3.12
winget install Docker.DockerDesktop
winget install k6
```
- After installing **Docker Desktop**, launch it from the Start menu once and let
  it finish starting (whale icon in the system tray goes steady). It needs
  **WSL2** — Docker Desktop will prompt you if it's missing.
- Close and reopen your terminal after installs so the `PATH` updates. If `k6`
  isn't found, its default path is `C:\Program Files\k6\k6.exe`.

### macOS (using Homebrew)
```bash
brew install python@3.12
brew install --cask docker        # then open Docker Desktop once
brew install k6
```

### Linux (Debian/Ubuntu)
```bash
sudo apt update && sudo apt install -y python3 python3-pip
# Docker Engine: follow https://docs.docker.com/engine/install/
# k6:
sudo gpg -k && \
sudo apt-key adv --keyserver keyserver.ubuntu.com --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69 && \
echo "deb https://dl.k6.io/deb stable main" | sudo tee /etc/apt/sources.list.d/k6.list && \
sudo apt update && sudo apt install k6
```

### Verify everything
```bash
python --version      # 3.12.x
docker --version      # 28.x (and Docker Desktop must be RUNNING)
docker ps             # should NOT error — if it does, start Docker Desktop
k6 version            # v2.x
```

---

## 4. Build it file by file

Create this structure inside a folder (e.g. `rate_limiter`):

```
rate_limiter/
├── gateway/
│   ├── token_bucket.lua     # the atomic rate-limit algorithm (runs in Redis)
│   ├── rate_limiter.py      # async wrapper around the Lua script
│   ├── app.py               # the gateway server
│   ├── requirements.txt
│   └── Dockerfile
├── upstream/
│   ├── app.py               # tiny backend
│   ├── requirements.txt
│   └── Dockerfile
├── load/
│   ├── loadtest.js          # k6 load test
│   └── burst.py             # quick concurrent smoke test
└── docker-compose.yml
```

> All these files already exist in this project — open each one and read the
> comments; they explain themselves line by line. Below is *why* each file
> matters and the key idea inside it.

### `gateway/token_bucket.lua` — the heart
The algorithm, written in Lua so Redis runs it atomically. It reads the client's
stored `tokens` + last timestamp, adds `elapsed × RATE` tokens (capped at
`BURST`), and if there's at least 1 token, spends it and returns `1` (allowed),
else returns `0` (denied). It also sets a TTL so idle clients' buckets expire and
Redis stays clean.

### `gateway/rate_limiter.py` — the Python wrapper
A `RateLimiter` class that connects to Redis, loads the Lua script **once**, and
exposes `async def allow(client_id) -> bool`. It also has a tiny self-test
(`python -m rate_limiter`) so you can verify the algorithm **in isolation** before
touching the web server — the easiest way to learn/debug it.

### `gateway/app.py` — the gateway server
An `aiohttp` app with a **middleware** (code that runs on *every* request before
the handler). The middleware:
1. figures out who the client is (`X-API-Key` header, else their IP),
2. calls `limiter.allow(...)`,
3. returns **429** if denied, otherwise **proxies** the request to the upstream
   and relays the response back.
`/health` is exempt so monitoring is never throttled.

### `upstream/app.py` — the backend
A trivial `aiohttp` server that replies `{"ok": true, ...}` to anything. Its only
job is to prove the gateway truly forwards traffic.

### `requirements.txt` files
Pin the library versions so everyone installs the same thing:
- gateway: `aiohttp==3.10.11`, `redis==5.2.1`
- upstream: `aiohttp==3.10.11`

### `Dockerfile`s
Each builds a small Python image: start from `python:3.12-slim`, install the
requirements, copy the code, expose the port, run the app.

### `docker-compose.yml`
Defines three services — `redis` (official image), `upstream`, `gateway` — on one
network. The gateway gets env vars `REDIS_URL`, `UPSTREAM_URL`, `RATE`, `BURST`,
and waits for Redis to be healthy before starting.

---

## 5. Run it

There are two ways. **Way A (Docker)** is the "one command" path and what we
recommend. **Way B (no Docker)** helps you see the moving parts.

### Way A — the whole stack with Docker Compose
From the project root:
```bash
docker compose up --build
```
This builds the images and starts redis + upstream + gateway. You'll see logs
from all three. The gateway listens on **http://localhost:8080**.

Leave it running and open a second terminal to test (next section). To stop:
press `Ctrl+C`, then `docker compose down` to remove the containers.

Run it in the background instead with `-d`:
```bash
docker compose up --build -d   # detached
docker compose ps              # see status
docker compose logs -f gateway # follow the gateway logs
docker compose down            # stop & clean up
```

### Way B — run pieces by hand (great for learning)
1. Start just Redis in Docker:
   ```bash
   docker compose up redis -d
   ```
2. In one terminal, start the upstream:
   ```bash
   cd upstream
   pip install -r requirements.txt
   python app.py            # listens on :8000
   ```
3. In another terminal, start the gateway pointing at both:
   ```bash
   cd gateway
   pip install -r requirements.txt
   # bash / macOS / Linux:
   REDIS_URL=redis://localhost:6379 UPSTREAM_URL=http://localhost:8000 python app.py
   # Windows PowerShell:
   #   $env:REDIS_URL="redis://localhost:6379"; $env:UPSTREAM_URL="http://localhost:8000"; python app.py
   ```
The gateway is now on :8080, just like Way A.

---

## 6. Test it & load test it

### Quick manual checks
```bash
curl localhost:8080/health     # {"status":"ok"}  (exempt from limiting)
curl localhost:8080/           # {"ok":true,...}  (proxied to upstream)
```

### See the limiter trip (concurrent burst)
A `curl` loop is too slow on Windows to drain the bucket (each `curl.exe` spawns a
new process ~20ms apart, slower than the refill). Use the included async helper:
```bash
python load/burst.py 500
# -> 500 concurrent requests -> {200: ~272, 429: ~228}
```
The 429s are the limiter rejecting overflow. **That's success.**

### Full load test with k6
The script `load/loadtest.js` has **two modes**:
```bash
# Stress test: ramps 0 -> 200 virtual users (finds the breaking point)
k6 run load/loadtest.js

# Steady test: hold a fixed number of users (measures real latency/throughput)
k6 run -e VUS=25 -e DURATION=15s load/loadtest.js
```

**Read these in the summary:**
- `http_reqs ........: 11189  931/s` → ~931 requests/second.
- `http_req_duration .: ... p(95)=39ms` → 95% of requests under 39ms. ✅ (<70ms)
- `rate_limited ......: 0.00%` → nothing throttled (limit had headroom).

**Headroom vs throttle runs** — change the limit when starting the gateway
(compose reads `${RATE}`/`${BURST}`):
```bash
# Headroom: raise the limit so you measure raw throughput, almost no 429s
RATE=1000000 BURST=1000000 docker compose up -d gateway
k6 run -e VUS=25 -e DURATION=15s load/loadtest.js

# Throttle: back to default 100/200 so the limiter clearly engages
docker compose up -d gateway
k6 run -e VUS=50 -e DURATION=12s load/loadtest.js   # ~50% rate_limited
```

**Capacity we measured** (one laptop, everything co-located, high limit):

| VUs | rps  | p95    | under 70ms? |
|-----|------|--------|-------------|
| 10  | ~800 | ~26ms  | ✅          |
| 25  | ~930 | ~39ms  | ✅ best spot |
| 50  | ~870 | ~79ms  | ❌ saturated |
| 200 | ~410 | ~510ms | ❌ overloaded |

**Key lesson:** beyond the sweet spot, adding load *lowers* throughput AND raises
latency — that's saturation/queueing, not a code bug. To reach numbers like
~2400 rps you need more CPU and, importantly, you run **k6 on a separate machine**
from the gateway so they don't compete for resources.

---

## 7. Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `bash: docker: command not found` right after a paste, with a weird `[200~` prefix | Your terminal's *bracketed paste* got pasted as text. Just retype the command. Docker is probably fine. |
| `docker ps` → `cannot find the file ... dockerDesktopLinuxEngine` | **Docker Desktop isn't running.** Launch it and wait for the whale icon to go steady, then retry. |
| `ConnectionError: Error ... connecting to localhost:6379` | **Redis isn't up.** Run `docker compose up redis -d` first. |
| A `curl` loop never shows 429s | Sequential curl is too slow to drain the bucket. Use `python load/burst.py 500` or k6 with enough VUs. |
| k6 prints `p(95)` huge (hundreds of ms) and a threshold fails | You **oversaturated** the gateway. Lower the VU count (try `-e VUS=25`). Don't run k6 + the stack on a tiny machine and expect big numbers. |
| `pip install` replaced my newer aiohttp | Expected — we pin `aiohttp==3.10.11` for reproducibility. Use a **virtual environment** (`python -m venv .venv`) to avoid touching your global packages. |
| Port 8080 already in use | Something else is on that port. Stop it, or change the gateway's published port in `docker-compose.yml`. |

> **Tip — use a virtual environment** so this project's libraries don't clash with
> others on your system:
> ```bash
> python -m venv .venv
> # Windows:  .venv\Scripts\activate     macOS/Linux:  source .venv/bin/activate
> pip install -r gateway/requirements.txt
> ```

---
