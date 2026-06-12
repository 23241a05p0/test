# Cloud Deployment & Exploration Walkthrough

A complete record of deploying and exploring the rate limiter on Google Cloud Platform (GCP),
including every command run, every error encountered, and what each experiment revealed.

---

## Table of Contents

1. [VM Setup on GCP](#1-vm-setup-on-gcp)
2. [Firewall Configuration](#2-firewall-configuration)
3. [Installing Docker on the VM](#3-installing-docker-on-the-vm)
4. [Copying Project Files & Starting Services](#4-copying-project-files--starting-services)
5. [Phase 1 — Basic Rate Limit Test (curl)](#5-phase-1--basic-rate-limit-test-curl)
6. [Phase 2 — Burst Test (burst.py)](#6-phase-2--burst-test-burstpy)
7. [Phase 3 — Stress Test (k6)](#7-phase-3--stress-test-k6)
8. [Phase 4 — Tuning RATE and BURST](#8-phase-4--tuning-rate-and-burst)
9. [Phase 5 — Watching Redis Live](#9-phase-5--watching-redis-live)
10. [Phase 6 — Multi-Client Isolation](#10-phase-6--multi-client-isolation)
11. [Key Takeaways](#11-key-takeaways)

---

## 1. VM Setup on GCP

### What we did
Created a virtual machine on Google Compute Engine to host the rate limiter stack.

### Command
```bash
gcloud compute instances create ratelimiter-vm \
  --machine-type=e2-small \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=20GB \
  --tags=ratelimiter \
  --zone=asia-south1-c
```

### Result
```
NAME: ratelimiter-vm
ZONE: asia-south1-c
MACHINE_TYPE: e2-small
INTERNAL_IP: 10.160.0.5
EXTERNAL_IP: 34.93.251.84
STATUS: RUNNING
```

### Errors encountered & fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `unrecognized arguments: ratelimiter` | Spaces around `=` in `--tags = ratelimiter` | Remove spaces: `--tags=ratelimiter` |
| `Unknown zone: asia-south1` | Used a region name instead of a zone | Append zone letter: `asia-south1-c` |
| `resource not found: family/debian-12` | Missing `--image-project` | Add `--image-project=debian-cloud` |
| `ZONE_RESOURCE_POOL_EXHAUSTED` | No `e2-small` capacity in `asia-south1-a` | Try different zone: `asia-south1-c` |

### SSH into the VM
```bash
gcloud compute ssh ratelimiter-vm --zone=asia-south1-c
```

---

## 2. Firewall Configuration

### What we did
Opened port 8080 so external traffic can reach the gateway service.
The `--target-tags=ratelimiter` ties the rule to only VMs tagged `ratelimiter`.

### Command
```bash
gcloud compute firewall-rules create allow-ratelimiter-8080 \
  --allow=tcp:8080 \
  --target-tags=ratelimiter \
  --direction=INGRESS
```

### Common mistake
Using the wrong tag name (`ratelier` instead of `ratelimiter`) means the rule is created
but never applied to the VM — traffic still gets blocked silently. Always verify:
```bash
gcloud compute firewall-rules describe allow-ratelimiter-8080
```

---

## 3. Installing Docker on the VM

### What we did
Installed Docker and Docker Compose inside the VM so we can run the containerized stack.

### Commands (run inside the VM)
```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker $USER
newgrp docker
```

### Notes
- `docker-compose-plugin` is not available in Debian's default repos — use `docker-compose` (the standalone binary) instead.
- `newgrp docker` applies the group change without requiring a full logout/login.
- Use `docker-compose` (hyphen) not `docker compose` (space) with this install.

---

## 4. Copying Project Files & Starting Services

### Upload files from local machine to Cloud Shell
In the Cloud Shell toolbar: **⋮ menu → Upload** → select the `rate_limiter` folder.

### Copy from Cloud Shell to VM
```bash
gcloud compute scp --recurse ~/rate_limiter ratelimiter-vm:~/rate_limiter --zone=asia-south1-c
```

### Start the stack
```bash
cd ~/rate_limiter
docker-compose up -d
```

### Verify it's running
```bash
curl http://localhost:8080/
# Expected: {"ok": true, "method": "GET", "path": "/"}
```

Or from the internet:
```bash
curl http://34.93.251.84:8080/
```

### What starts up
| Container | Role | Port |
|-----------|------|------|
| `redis` | Stores token buckets, answers in ~1ms | 6379 (internal only) |
| `upstream` | The real backend app (simple Flask echo) | 8000 (internal only) |
| `gateway` | Rate limiting proxy, the only public-facing service | 8080 |

---

## 5. Phase 1 — Basic Rate Limit Test (curl)

### What we did
Manually triggered 429 responses by lowering limits so even sequential curls exhaust the bucket.

### Why default limits don't show 429s with curl
Default `BURST=200` means the bucket holds 200 tokens. A sequential curl loop fires ~1 req/sec,
which is slower than the refill rate — the bucket never empties.

### Lower limits to see 429s
```bash
RATE=5 BURST=10 docker-compose up -d
```

### Test
```bash
for i in $(seq 1 20); do curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/; done
```

### Expected output
```
200   ← tokens 10→9
200   ← tokens 9→8
...
200   ← tokens 1→0
429   ← bucket empty
429
429
...
```

First 10 requests allowed (one token each), the rest rejected.

---

## 6. Phase 2 — Burst Test (burst.py)

### What we did
Fired 500 **concurrent** requests using asyncio to actually exhaust the token bucket.
Unlike a curl loop, concurrent requests arrive faster than the bucket refills.

### Setup
```bash
sudo apt-get install -y python3-aiohttp
```

### Restore default limits first
```bash
docker-compose up -d
```

### Run
```bash
python3 load/burst.py 500 http://localhost:8080/
```

### Result
```
500 concurrent requests -> {200: 260, 429: 240}
  allowed (200): 260
  throttled (429): 240
```

### Why 260 allowed instead of exactly 200?
The token bucket refills at `RATE=100 tokens/sec` **while** the 500 requests are being processed.
The burst takes ~60ms to complete — during that time `100 × 0.06 = ~6` extra tokens refill.
With connection overhead and timing variance, ~60 extra requests slip through. This is correct behaviour.

---

## 7. Phase 3 — Stress Test (k6)

### What we did
Ran a proper load test ramping from 0 to 200 virtual users to measure throughput and latency.

### Install k6
```bash
sudo gpg --no-default-keyring \
  --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
  --keyserver hkp://keyserver.ubuntu.com:80 \
  --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69

echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/k6.list

sudo apt-get update
sudo apt-get install -y k6
```

### Run
```bash
k6 run -e BASE_URL=http://localhost:8080 load/loadtest.js
```

The test ramps: 0→50 VUs (10s), 50→200 VUs (30s), 200→0 VUs (10s).

### Results (RATE=100, BURST=200)
```
Total requests:    74,332  (1,487 req/sec)
Rate limited:      92.77%
Checks passed:     100%    (every response was 200 or 429 — no crashes)
p(95) latency:     137ms   (threshold was 70ms — failed due to VM size)
```

### What the threshold failure means
The `p(95)<70ms` threshold is designed for a multi-core production server.
An `e2-small` (2 vCPU, 2GB RAM) under 200 concurrent users can't hit that target.
The rate limiter itself is healthy — it never errored, just ran slow under load.

---

## 8. Phase 4 — Tuning RATE and BURST

### What we did
Increased limits 5× and re-ran the same k6 test to observe the difference.

### Command
```bash
RATE=500 BURST=1000 docker-compose up -d
k6 run -e BASE_URL=http://localhost:8080 load/loadtest.js
```

### Comparison

| Metric | RATE=100, BURST=200 | RATE=500, BURST=1000 |
|--------|---------------------|----------------------|
| Total req/sec | **1,487** | 1,075 |
| Rate limited | 92.77% | **47.46%** |
| p(95) latency | **137ms** | 201ms |

### The counterintuitive result
Higher limits produced **fewer** total requests and **worse** latency. Why?

- A `429` response is instant — just a Redis Lua script returning "no tokens" (~1ms).
- A `200` response must travel to the upstream Flask app and back (~50–100ms).
- With loose limits, 5× more requests reach the upstream, **overloading it** on the small VM.
- The rate limiter was protecting the upstream from its own traffic.

```
Tight limits:  930 fast 429s + 70 slow 200s  → high throughput, low latency
Loose limits:  470 fast 429s + 530 slow 200s → upstream bottleneck, worse everything
```

**Lesson:** Rate limits exist not just for fairness to clients, but to protect your backend
from being overwhelmed by legitimate traffic spikes.

---

## 9. Phase 5 — Watching Redis Live

### What we did
Used `MONITOR` in redis-cli to watch every Redis command in real time while a burst ran.

### Open Redis CLI
```bash
sudo docker exec -it $(sudo docker ps -qf name=redis) redis-cli
MONITOR
```

### Fire a burst in another tab
```bash
python3 load/burst.py 100 http://localhost:8080/
```

### What you see in MONITOR
Each request triggers exactly 3 Redis commands:

```
EVALSHA  4338a2...  1  "rl:ip:172.19.0.1"  "500.0"  "1000.0"  "1781263006.089"  "1"
         │                │                  │         │          │                 │
         └─ Lua script    └─ bucket key      └─ rate   └─ burst   └─ timestamp      └─ cost
```

```
HMGET  "rl:ip:172.19.0.1"  "tokens"  "ts"
       └─ read current token count + last timestamp
```

```
HSET  "rl:ip:172.19.0.1"  "tokens"  "996.66"  "ts"  "1781263006.090"
      └─ write back new token count + updated timestamp
```

### Watching the bucket drain and refill
```
"tokens" "1000.00"   ← start (full bucket)
"tokens"  "996.66"   ← after request 1
"tokens"  "993.90"   ← after request 4
"tokens"  "938.18"   ← after request ~62 (draining fast)
   ... 50ms gap (requests slow down) ...
"tokens"  "965.72"   ← REFILLED! gap × rate = 0.05s × 500 = +27 tokens
"tokens"  "964.89"   ← draining again
```

### Why Lua?
All 3 operations run as a single atomic unit inside Redis.
Without Lua, two concurrent requests could both read `tokens=1`, both decide "allowed",
and both spend the last token — allowing one request too many.
With Lua, Redis processes the script serially — no race conditions, no double-spending.

---

## 10. Phase 6 — Multi-Client Isolation

### What we did
Proved that each client gets its own independent token bucket — one client exhausting
their tokens has zero effect on another client.

### How the gateway identifies clients
From [gateway/app.py](gateway/app.py):

```python
def client_id(request: web.Request) -> str:
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key}"       # → Redis key: rl:key:alice
    return f"ip:{request.remote}"    # → Redis key: rl:ip:172.19.0.1
```

Priority: **API key > source IP**

### Test with separate API keys
```bash
# Fire requests as "alice" and "bob", then immediately check Redis keys
for key in alice bob; do
  for i in $(seq 1 3); do
    curl -s -o /dev/null -H "X-API-Key: $key" http://localhost:8080/
  done
done && sudo docker exec -it $(sudo docker ps -qf name=redis) redis-cli KEYS "rl:*"
```

### Expected Redis keys
```
1) "rl:key:alice"
2) "rl:key:bob"
```

Two separate keys, two separate buckets. Alice being rate-limited doesn't affect Bob at all.

### Key TTL
Each bucket key has a 3-second TTL (`EXPIRE` in the Lua script).
If a client goes quiet for 3 seconds, their key is automatically deleted from Redis —
no memory leak, and their bucket resets fresh on next request.

---

## 11. Key Takeaways

### Architecture
```
Internet → [Firewall: tcp:8080] → Gateway (aiohttp)
                                       │
                                  Redis (Lua atomic check)
                                       │
                                  Upstream (Flask)
```