// k6 load test for the API Gateway.
//
// Run:  k6 run load/loadtest.js
//
// What it does: ramps virtual users (VUs) up and down while hammering the
// gateway, then prints a summary with requests/sec (http_reqs) and latency
// percentiles (http_req_duration). The `thresholds` below make k6 PASS/FAIL
// the run automatically.
//
// Tip: each VU sends requests as fast as it gets responses, so more VUs => more
// rps. Bump the `target` numbers to push toward ~2400 rps on your machine.

import http from "k6/http";
import { check } from "k6";
import { Rate } from "k6/metrics";

// Custom metric: fraction of responses that were 429 (rate-limited).
const rateLimited = new Rate("rate_limited");

const BASE = __ENV.BASE_URL || "http://localhost:8080";

// Two modes:
//   default        -> ramp 0..200 VUs (stress / find the breaking point)
//   VUS + DURATION  -> hold a constant VU count (measure steady latency/rps)
// e.g.  k6 run -e VUS=25 -e DURATION=15s load/loadtest.js
const VUS = __ENV.VUS ? parseInt(__ENV.VUS) : null;
const DURATION = __ENV.DURATION || "15s";

export const options = VUS
  ? {
      vus: VUS,
      duration: DURATION,
      thresholds: { http_req_duration: ["p(95)<70"] },
    }
  : {
      stages: [
        { duration: "10s", target: 50 },   // ramp up
        { duration: "30s", target: 200 },  // hold load
        { duration: "10s", target: 0 },    // ramp down
      ],
      thresholds: { http_req_duration: ["p(95)<70"] },
    };

export default function () {
  const res = http.get(`${BASE}/`);

  // A request is "good" if it was either served (200) or correctly throttled (429).
  check(res, {
    "status is 200 or 429": (r) => r.status === 200 || r.status === 429,
  });
  rateLimited.add(res.status === 429);
}
