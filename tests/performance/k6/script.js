import http from "k6/http";
import { check } from "k6";

// A liveness/latency check on porth's REST /health, not a throughput test of
// its actual job: a submit queues an SMS for a real SMSC, which k6 doesn't
// drive. /health answers from the in-process queues, without the database.
//
// Run against a live instance: `make run` (or bin/porth config/config.toml),
// then from the repository root:
//   BASE_URL=http://localhost:8080 k6 run tests/performance/k6/script.js

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";

export const options = {
  vus: 5,
  duration: "15s",
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<100"],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/health`);
  check(res, { "status is 200": (r) => r.status === 200 });
}
