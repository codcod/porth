import http from "k6/http";
import { check } from "k6";

// A liveness/latency check on porth's REST /health, not a throughput test of
// its actual job: a submit queues an SMS for a real SMSC, which k6 doesn't
// drive. /health answers from the in-process queues, without the database.
//
// Run against a live instance: `make run` (or bin/porth config/config.toml),
// then from the repository root:
//   BASE_URL=http://localhost:8080 k6 run tests/performance/k6/script.js
//
// SEND=1 submits SMS instead, so /admin's counters move. Only against an SMSC
// that's safe to flood, such as compose.demo.yaml's simulator: every request
// queues a real message.

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
  const res = __ENV.SEND
    ? http.post(
        `${BASE_URL}/api/v1/sms/send`,
        JSON.stringify({ from_number: "ACME", to_number: "306900000000", message: "k6" }),
        { headers: { "Content-Type": "application/json" } },
      )
    : http.get(`${BASE_URL}/health`);
  check(res, { "status is 200": (r) => r.status === 200 });
}
