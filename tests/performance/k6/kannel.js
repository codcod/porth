import http from "k6/http";
import { check } from "k6";

// script.js over porth's Kannel-compatible API: each request is a sendsms that queues a
// real SMS, so run it only against an SMSC that's safe to flood, such as the simulators
// in compose-demo-simple.yaml (publish 13013 first) or compose-demo-advanced.yaml:
//   make perf-kannel        # or: k6 run tests/performance/k6/kannel.js
//
// The demos configure no kannel.users, so no username/password is sent. POST=1 sends the
// POST form instead: the text as the body, the parameters as X-Kannel-* headers.

const BASE_URL = __ENV.BASE_URL || "http://localhost:13013";

export const options = {
  vus: 5,
  duration: "15s",
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<100"],
  },
};

export default function () {
  const res = __ENV.POST
    ? http.post(`${BASE_URL}/cgi-bin/sendsms`, "k6", {
        headers: { "Content-Type": "text/plain", "X-Kannel-From": "ACME", "X-Kannel-To": "306900000000" },
      })
    : http.get(`${BASE_URL}/cgi-bin/sendsms?from=ACME&to=306900000000&text=k6`);
  check(res, {
    "status is 202": (r) => r.status === 202,
    "accepted": (r) => r.body.startsWith("0: Accepted for delivery"),
  });
}
