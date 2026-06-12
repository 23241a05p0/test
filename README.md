# Async API Gateway with Redis Token-Bucket Rate Limiter

An asynchronous API Gateway in Python (`asyncio` + `aiohttp`) that proxies traffic
to a backend service, protected by a **Redis-backed token-bucket rate limiter**.
Containerized with Docker Compose and load-tested with k6.

```
  k6 (load test)  -->  Gateway (aiohttp, async, :8080)  -->  Upstream (aiohttp, :8000)
                            |
                            v
                         Redis  (token-bucket counters, atomic Lua script)
```

## How it works (the short version)

- **Gateway** (`gateway/app.py`): every request hits a middleware that identifies the
  client (`X-API-Key` header, else source IP), asks the rate limiter for a token, and
  either **proxies** the request upstream (200) or rejects it with **429 Too Many Requests**.
- **Token bucket** (`gateway/token_bucket.lua` + `gateway/rate_limiter.py`): each client has
  a bucket that refills at `RATE` tokens/sec up to `BURST` capacity. Each request spends one
  token. The refill-and-spend runs as a single **Lua script inside Redis**, so concurrent
  requests can't race each other.
- **Upstream** (`upstream/app.py`): a trivial echo service, so the gateway is really forwarding
  traffic, not answering itself.

## Configuration (env vars on the `gateway` service)

| Var           | Default                  | Meaning                          |
|---------------|--------------------------|----------------------------------|
| `RATE`        | `100`                    | tokens added per second / client |
| `BURST`       | `200`                    | max tokens (burst size) / client |
| `REDIS_URL`   | `redis://redis:6379`     | Redis connection                 |
| `UPSTREAM_URL`| `http://upstream:8000`   | backend to proxy to              |
| `PORT`        | `8080`                   | gateway listen port              |

## Run the whole stack

```bash
docker compose up --build
```

Then in another terminal:

```bash
curl localhost:8080/health      # -> {"status":"ok"}   (exempt from limiting)
curl localhost:8080/            # -> {"ok":true,...}    (proxied to upstream)

# Exceed the burst to see throttling kick in.
# NOTE: a curl loop on Windows is too SLOW to trip the limiter — spawning one
# curl.exe per request takes ~20ms, so RATE refills the bucket faster than you
# drain it. Use the concurrent Python helper instead:
python load/burst.py 500
# -> 500 concurrent requests -> {200: ~272, 429: ~228}
# The 429s prove the limiter engages once the bucket empties.
```

## Test the limiter in isolation (good for debugging)

With just Redis running (`docker compose up redis`), from `gateway/`:

```bash
pip install -r requirements.txt
REDIS_URL=redis://localhost:6379 python -m rate_limiter
# prints how many of 20 instant requests were allowed vs denied, then refills.
```

## Load test with k6

Install k6 (https://k6.io/docs/get-started/installation/), then:

```bash
k6 run load/loadtest.js
```

### Two test modes (the script reads env vars)

```bash
# Stress / find the breaking point: ramps 0 -> 200 VUs
k6 run load/loadtest.js

# Steady measurement: hold a constant number of virtual users
k6 run -e VUS=25 -e DURATION=15s load/loadtest.js
```

### Reading the k6 summary

- **`http_reqs`** — total requests and the **requests/sec** rate (your throughput).
- **`http_req_duration`** — latency. Look at **`p(95)`**; the threshold fails the run if it
  exceeds **70ms**.
- **`rate_limited`** — custom metric: the fraction of responses that are 429. Seeing some
  under heavy load means the limiter is doing its job.

### Important: don't oversaturate, and don't co-locate the load generator

Latency explodes when you push **more load than the gateway can serve** — the requests queue,
and that queue *is* the latency. Also, running k6 + gateway + upstream + Redis on one machine
makes them fight for CPU. To measure real capacity, raise the VU count gradually and watch where
`p(95)` crosses 70ms.

Measured on one Windows laptop (Docker Desktop, everything co-located), high `RATE` so nothing
throttles:

| VUs | rps  | p95    | <70ms |
|-----|------|--------|-------|
| 10  | ~800 | ~26ms  | ✅    |
| 25  | ~930 | ~39ms  | ✅ (sweet spot) |
| 50  | ~870 | ~79ms  | ❌ (saturated) |
| 200 | ~410 | ~510ms | ❌ (badly overloaded) |

Higher numbers like ~2400 rps come from better hardware and, crucially, running the load
generator on a **separate machine** from the system under test.

### Demonstrating throttling vs throughput

Override the limit when starting the stack (compose reads `${RATE}`/`${BURST}`):

```bash
RATE=1000000 BURST=1000000 docker compose up -d gateway   # headroom: measure raw throughput
docker compose up -d gateway                              # back to default 100/200: see 429s
```

## Project layout

```
gateway/    aiohttp gateway, rate limiter, Lua script, Dockerfile
upstream/   tiny echo backend, Dockerfile
load/       k6 load test script
docker-compose.yml
```
