# Deploying to Google Cloud (Compute Engine VM + Docker Compose)

This guide puts your rate-limiter stack on a real cloud server. We use a single
**Compute Engine VM**, install Docker, and run the **same `docker-compose.yml`**
you already have. It's the smallest leap from local, the cheapest option, and you
learn the fundamentals (a server, a firewall, a public IP) without Kubernetes.

> **Assumes:** you have a Google Cloud account with billing enabled. Costs are
> small (a few cents/hour) **but not zero** — see [§8 Cost & cleanup](#8-cost--cleanup).
> Always delete the VM when you're done.

```
Your laptop (curl / k6)  ──internet──►  GCP VM (public IP, port 8080)
                                          └─ docker compose: gateway + upstream + redis
```

---

## 0. One-time setup: the gcloud CLI

You can run every command below in two places. Pick one:

**Option A — Cloud Shell (easiest, nothing to install).** Open
https://console.cloud.google.com and click the terminal icon (`>_`) top-right. It
has `gcloud` preinstalled and you're already logged in. *(Note: to run k6 against
your VM you'll still want it on your laptop, but all the deploy commands work in
Cloud Shell.)*

**Option B — install gcloud on your laptop:**
```bash
# Windows:
winget install Google.CloudSDK
# macOS:
brew install --cask google-cloud-sdk
# then authenticate:
gcloud auth login
```

### Set your project and a default zone
```bash
gcloud projects list                       # find your PROJECT_ID
gcloud config set project YOUR_PROJECT_ID
gcloud config set compute/zone us-central1-a   # pick a zone near you
```
Enable the Compute Engine API (one time per project):
```bash
gcloud services enable compute.googleapis.com
```

---

## 1. Harden the compose file before going public ⚠️

Locally, `docker-compose.yml` publishes Redis on `6379:6379`. On a server with a
public IP that would expose Redis to the internet — a classic way to get hacked.
**Redis only needs to be reachable by the gateway container**, which talks to it
over the private Docker network (`redis://redis:6379`), *not* via the host port.

Edit `docker-compose.yml` and change the redis ports so it's **not** published to
the outside (bind to localhost only, or remove the mapping entirely):
```yaml
  redis:
    image: redis:7-alpine
    ports:
      - "127.0.0.1:6379:6379"   # localhost only — NOT reachable from the internet
```
The `upstream` service is already safe (it never publishes a host port — only the
gateway's `8080` is public). Good.

---

## 2. Create the VM

We'll use a small, cheap **e2-small** (2 vCPU burst, 2 GB RAM) running Debian 12.
That's enough to build images and run the three containers.

```bash
gcloud compute instances create ratelimiter-vm \
  --machine-type=e2-small \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=20GB \
  --tags=ratelimiter
```
- `--tags=ratelimiter` labels the VM so our firewall rule (next step) targets it.
- Cheaper option: `e2-micro` is in the always-free tier in some US regions, but
  2 GB+ RAM (`e2-small`) builds Docker images more reliably. Your call.

Note the **EXTERNAL_IP** printed in the output (or fetch it later):
```bash
gcloud compute instances describe ratelimiter-vm \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

---

## 3. Open the firewall for port 8080

By default GCP blocks inbound traffic. Allow TCP 8080 to VMs tagged
`ratelimiter`. **For safety, scope it to your own IP** rather than the whole
internet:

```bash
# find your laptop's public IP: visit https://ifconfig.me  (or run: curl ifconfig.me)
gcloud compute firewall-rules create allow-ratelimiter-8080 \
  --allow=tcp:8080 \
  --target-tags=ratelimiter \
  --source-ranges=YOUR.LAPTOP.IP.HERE/32
```
- To let *anyone* reach it (e.g. for a demo), use `--source-ranges=0.0.0.0/0`
  instead — but then only run it while you're watching, and never expose Redis.

---

## 4. Install Docker on the VM

SSH into the machine (this opens a shell *on the VM*):
```bash
gcloud compute ssh ratelimiter-vm
```
Now, **on the VM**, install Docker Engine + the Compose plugin:
```bash
# official convenience script — fine for a learning box
curl -fsSL https://get.docker.com | sudo sh

# run docker without sudo (log out/in after, or run `newgrp docker`)
sudo usermod -aG docker $USER
newgrp docker

# verify
docker --version
docker compose version
```

---

## 5. Get your code onto the VM

Two ways — pick one.

**Option A — via Git (best if your project is on GitHub):**
```bash
# on the VM:
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/YOUR_USERNAME/rate_limiter.git
cd rate_limiter
```

**Option B — copy straight from your laptop with scp** (run this **on your
laptop**, not the VM — open a second terminal):
```bash
gcloud compute scp --recurse ./rate_limiter ratelimiter-vm:~/rate_limiter
```
Then back on the VM: `cd ~/rate_limiter`.

> Make sure the `docker-compose.yml` you bring has the Redis port hardened from
> [§1](#1-harden-the-compose-file-before-going-public-).

---

## 6. Run the stack

On the VM, from the project folder:
```bash
docker compose up --build -d      # build images and start in the background
docker compose ps                 # all three should be Up; redis "healthy"
docker compose logs -f gateway    # watch logs (Ctrl+C to stop watching)
```
First build takes a couple of minutes (downloading Python image + libs).

Quick check **on the VM** (localhost works there):
```bash
curl localhost:8080/health        # {"status":"ok"}
curl localhost:8080/              # {"ok":true,...}
```

---

## 7. Test it from the outside

Now from **your laptop**, using the VM's external IP:
```bash
export VM=YOUR_EXTERNAL_IP
curl http://$VM:8080/health       # {"status":"ok"}  — reached the cloud! 🎉
curl http://$VM:8080/             # {"ok":true,...}
```

### Load test the cloud VM with k6 (the "real" setup)
Because k6 now runs on a **different machine** (your laptop) from the gateway
(the VM), you avoid the local "everything fights for CPU" problem — these numbers
are more honest. Point the existing script at the VM with the `BASE_URL` env the
script already supports:
```bash
k6 run -e BASE_URL=http://$VM:8080 -e VUS=25 -e DURATION=20s load/loadtest.js
```
Watch `http_reqs` (throughput) and `http_req_duration p(95)` (should be low if the
VM isn't saturated). Bump `VUS` to find this VM's breaking point — a bigger
`--machine-type` will push the numbers higher.

> Note: latency now includes **internet round-trip time** to the GCP region, so
> pick a zone near you in [§0](#0-one-time-setup-the-gcloud-cli) for the best p95.

### See throttling in the cloud
Same trick as locally — restart the gateway with a high limit for a headroom run,
then back to default to watch 429s appear:
```bash
# on the VM:
RATE=1000000 BURST=1000000 docker compose up -d gateway   # headroom
docker compose up -d gateway                              # default 100/200 -> 429s
```

---

## 8. Cost & cleanup

A running `e2-small` costs roughly **a few US cents per hour** plus a little for
the disk and network. It is **not free** while running. Two habits:

**Stop the VM when you're not using it** (keeps the disk, stops compute charges):
```bash
gcloud compute instances stop ratelimiter-vm
gcloud compute instances start ratelimiter-vm   # start again later (new IP unless reserved)
```

**Delete everything when you're done** (stops all charges):
```bash
gcloud compute instances delete ratelimiter-vm
gcloud compute firewall-rules delete allow-ratelimiter-8080
```
Optionally set a **budget alert** in the console (Billing → Budgets & alerts) so
you get emailed if spend crosses, say, $5.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `curl http://$VM:8080` from laptop times out | Firewall rule missing or scoped to the wrong IP. Recheck [§3](#3-open-the-firewall-for-port-8080); confirm your laptop's public IP (it changes on different networks/Wi-Fi). |
| Works from VM (`localhost`) but not from laptop | Same as above — it's the firewall/IP, not the app. |
| `docker compose up` killed / out of memory during build | VM too small. Recreate with `--machine-type=e2-medium` (4 GB RAM). |
| `permission denied` on docker commands | You skipped `newgrp docker` (or didn't log out/in) after `usermod -aG docker`. |
| External IP changed after stop/start | Normal for ephemeral IPs. Reserve a static IP (`gcloud compute addresses create`) if you need it stable. |
| Redis reachable from internet | You didn't apply [§1](#1-harden-the-compose-file-before-going-public-). Fix the compose ports and `docker compose up -d` again. |

---

## 10. Where to go next
- **HTTPS + a domain** — put the gateway behind a managed load balancer or
  Caddy/Nginx with a Let's Encrypt cert so it's `https://`.
- **Startup script** — pass a `--metadata-from-file startup-script=...` to the VM
  so Docker installs and the stack launches automatically on boot (infra-as-code).
- **Container-Optimized OS / Cloud Run / GKE** — once comfortable, try the more
  managed, auto-scaling options for the same containers.
- **Reserve a static IP** and add monitoring (Cloud Monitoring) for a
  production-feeling setup.

---

You now have the stack running on real cloud infrastructure, reachable over the
internet, load-tested from a separate machine — a genuine end-to-end deployment.
Remember: **stop or delete the VM when you're done** so it doesn't quietly bill you.
