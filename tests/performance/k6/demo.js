import http from "k6/http";
import { check } from "k6";

// Load through compose-demo.yaml: each iteration sends one SMS over REST and one over
// Kannel's sendsms. Every send queues a real message, so run it only against an SMSC
// that's safe to flood, such as the demo's simulator:
//   make perf-demo          # or: k6 run tests/performance/k6/demo.js
//
// POST=1 sends Kannel's POST form instead: the text as the body, the parameters as
// X-Kannel-* headers.
//
// AUTH=1 is for the compose-demo-auth.yaml overlay (make perf-demo-auth): both sends carry
// credentials, and each iteration also sends one of each without, which must be refused.
// TOKEN, KANNEL_USER and KANNEL_PASSWORD default to the overlay's values.

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const KANNEL_URL = __ENV.KANNEL_URL || "http://localhost:13013";
const AUTH = !!__ENV.AUTH;
const TOKEN = __ENV.TOKEN || "demo-token-0123456789abcdef";
const KANNEL_USER = __ENV.KANNEL_USER || "acme";
const KANNEL_PASSWORD = __ENV.KANNEL_PASSWORD || "demo-secret";

const SMS = JSON.stringify({ from_number: "ACME", to_number: "306900000000", message: "k6" });
const JSON_TYPE = { "Content-Type": "application/json" };
const BEARER = AUTH ? { Authorization: `Bearer ${TOKEN}` } : {};
const LOGIN = AUTH ? { username: KANNEL_USER, password: KANNEL_PASSWORD } : {};

export const options = {
  vus: 5,
  duration: "15s",
  thresholds: {
    // a refusal counts as a success here: each refused request expects its 401/403
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<100"],
  },
};

function sendsms(login, params) {
  if (__ENV.POST) {
    const headers = { "Content-Type": "text/plain", "X-Kannel-From": "ACME", "X-Kannel-To": "306900000000" };
    if (login.username) {
      headers["X-Kannel-Username"] = login.username;
      headers["X-Kannel-Password"] = login.password;
    }
    return http.post(`${KANNEL_URL}/cgi-bin/sendsms`, "k6", { headers, ...params });
  }
  const query = Object.entries({ ...login, from: "ACME", to: "306900000000", text: "k6" })
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
    .join("&");
  return http.get(`${KANNEL_URL}/cgi-bin/sendsms?${query}`, params);
}

export default function () {
  const rest = http.post(`${BASE_URL}/api/v1/sms/send`, SMS, { headers: { ...JSON_TYPE, ...BEARER } });
  check(rest, { "REST send is 200": (r) => r.status === 200 });

  const kannel = sendsms(LOGIN, {});
  check(kannel, {
    "sendsms is 202": (r) => r.status === 202,
    "accepted": (r) => r.body.startsWith("0: Accepted for delivery"),
  });

  if (!AUTH) return;

  const restRefused = http.post(`${BASE_URL}/api/v1/sms/send`, SMS, {
    headers: JSON_TYPE,
    responseCallback: http.expectedStatuses(401),
  });
  check(restRefused, { "REST without token is 401": (r) => r.status === 401 });

  const kannelRefused = sendsms({}, { responseCallback: http.expectedStatuses(403) });
  check(kannelRefused, { "sendsms without password is 403": (r) => r.status === 403 });
}
