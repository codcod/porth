import http from "k6/http";
import exec from "k6/execution";
import { check, sleep } from "k6";

// Load through every part of compose-demo-advanced.yaml: REST sends routed to all three
// SMSCs, status polls, Kannel sendsms with smsc= forcing a route, MO through each
// simulator, and /health. Every send queues a real message: run it only against the
// demo's simulators.
//
//   docker compose -f compose-demo-advanced.yaml up -d
//   until curl -sf localhost:8080/ready; do sleep 1; done
//   make perf-advanced      # or: k6 run tests/performance/k6/advanced.js
//
// The run fails if any SMSC reaches no more final statuses (porth_messages_final) than
// it had before the run, i.e. routing sent it nothing.

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const KANNEL_URL = __ENV.KANNEL_URL || "http://localhost:13013";
// t-mobile, orange-1, orange-2
const MO_URLS = (__ENV.MO_URLS || "http://localhost:2776,http://localhost:2777,http://localhost:2778").split(",");
const SMSCS = ["t-mobile", "orange-1", "orange-2"];
const JSON_HEADERS = { headers: { "Content-Type": "application/json" } };

export const options = {
  scenarios: {
    rest_send: {
      executor: "constant-arrival-rate", exec: "restSend",
      rate: 30, timeUnit: "1s", duration: "30s", preAllocatedVUs: 20,
    },
    status_poll: { executor: "constant-vus", exec: "statusPoll", vus: 3, duration: "30s" },
    kannel_forced: {
      executor: "constant-arrival-rate", exec: "kannelForced",
      rate: 2, timeUnit: "1s", duration: "30s", preAllocatedVUs: 2,
    },
    mo: {
      executor: "constant-arrival-rate", exec: "mo",
      rate: MO_URLS.length, timeUnit: "1s", duration: "30s", preAllocatedVUs: 3,
    },
    health: { executor: "constant-vus", exec: "health", vus: 1, duration: "30s" },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{scenario:rest_send}": ["p(95)<300"],
    "http_req_duration{scenario:status_poll}": ["p(95)<300"],
    "http_req_duration{scenario:kannel_forced}": ["p(95)<300"],
    "http_req_duration{scenario:mo}": ["p(95)<300"],
    "http_req_duration{scenario:health}": ["p(95)<100"],
    // "check" is a k6 system tag (the check's name), so the custom tag needs another name
    "checks{routing:1}": ["rate==1"],
  },
};

// Half to Poland (t-mobile), a quarter to 49 (orange-2), the rest to the default (orange-1)
function destination() {
  const r = Math.random();
  if (r < 0.5) return "48600000000";
  if (r < 0.75) return "49151000000";
  return r < 0.875 ? "306900000000" : "447700900000";
}

function send() {
  const res = http.post(
    `${BASE_URL}/api/v1/sms/send`,
    JSON.stringify({ from_number: "ACME", to_number: destination(), message: "k6" }),
    JSON_HEADERS,
  );
  check(res, {
    "send is 200": (r) => r.status === 200,
    "send has a message_id": (r) => !!r.json("message_id"),
  });
  return res.status === 200 ? res.json("message_id") : null;
}

// porth_messages_final_total per SMSC, all statuses together
function finals() {
  const sums = Object.fromEntries(SMSCS.map((s) => [s, 0]));
  const body = http.get(`${BASE_URL}/metrics`).body;
  for (const line of body.split("\n")) {
    const m = line.match(/^porth_messages_final_total\{.*smsc="([^"]+)".*\} (\S+)$/);
    if (m && m[1] in sums) sums[m[1]] += parseFloat(m[2]);
  }
  return sums;
}

export function setup() {
  return finals();
}

export function restSend() {
  send();
}

// VUs share no memory, so one iteration sends and then polls its own message
export function statusPoll() {
  const id = send();
  sleep(3);
  if (!id) return;
  const res = http.get(`${BASE_URL}/api/v1/sms/status/${id}`);
  check(res, {
    "status is 200": (r) => r.status === 200,
    "status is known": (r) =>
      ["pending", "queued", "sent", "delivered", "failed", "expired"].includes(r.json("status")),
  });
}

export function kannelForced() {
  const res = http.get(`${KANNEL_URL}/cgi-bin/sendsms?from=ACME&to=306900000000&text=k6&smsc=orange-2`);
  check(res, {
    "sendsms is 202": (r) => r.status === 202,
    "sendsms accepted": (r) => r.body.startsWith("0: Accepted for delivery"),
  });
}

export function mo() {
  const url = MO_URLS[exec.scenario.iterationInTest % MO_URLS.length];
  const res = http.post(`${url}/mo?from=306900000000&to=ACME&text=k6`);
  check(res, { "mo is 200": (r) => r.status === 200 });
}

export function health() {
  check(http.get(`${BASE_URL}/health`), { "health is 200": (r) => r.status === 200 });
  sleep(0.5);
}

export function teardown(before) {
  sleep(5); // the last receipts
  const after = finals();
  for (const smsc of SMSCS) {
    check(after[smsc] - before[smsc], { [`${smsc} reached final statuses`]: (n) => n > 0 }, { routing: "1" });
  }
}
