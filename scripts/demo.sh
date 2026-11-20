#!/usr/bin/env bash
# Demo: compress tables (fits / over capacity), then diagnose which pending
# routes a swapped-in device actually installed (decided / conflict).
# Usage: ./scripts/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://localhost:8000}"

read -r -d '' ROUTES <<'JSON' || true
[
  {"prefix": "10.0.0.0/8", "action": "NH1"},
  {"prefix": "10.1.0.0/16", "action": "NH2"},
  {"prefix": "10.2.0.0/16", "action": "NH1"},
  {"prefix": "10.3.0.0/16", "action": "drop"},
  {"prefix": "192.168.0.0/16", "action": "NH2"}
]
JSON

echo "=== 1) capacity sufficient (capacity=4, drill: NH2 fails) ==="
curl -sS -X POST "$BASE/compress" -H 'Content-Type: application/json' -d "{
  \"routes\": $ROUTES,
  \"capacity\": 4,
  \"failed_nexthop\": \"NH2\"
}" | python3 -m json.tool

echo
echo "=== 2) capacity insufficient (capacity=3): only the minimum count is reported ==="
curl -sS -X POST "$BASE/compress" -H 'Content-Type: application/json' -d "{
  \"routes\": $ROUTES,
  \"capacity\": 3,
  \"failed_nexthop\": \"NH2\"
}" | python3 -m json.tool

echo
echo "=== 3) diagnose: which pending routes did the swapped-in device actually install? ==="
curl -sS -X POST "$BASE/diagnose" -H 'Content-Type: application/json' -d '{
  "routes": [
    {"prefix": "10.0.0.0/8", "action": "NH1", "status": "confirmed"},
    {"prefix": "10.1.0.0/16", "action": "NH2", "status": "pending"},
    {"prefix": "10.2.0.0/16", "action": "NH2", "status": "pending"},
    {"prefix": "10.3.0.0/16", "action": "drop", "status": "pending"},
    {"prefix": "192.168.0.0/16", "action": "NH3", "status": "confirmed"}
  ],
  "snapshot": {
    "installed_count": 4,
    "observations": [
      {"address": "10.1.2.3", "result": "NH2"},
      {"address": "10.3.2.1", "result": "NH1"},
      {"address": "192.168.9.9", "result": "NH3"},
      {"address": "203.0.113.5", "result": "no-match"}
    ]
  }
}' | python3 -m json.tool

echo
echo "=== 4) diagnose: contradictory evidence -> conflict only ==="
curl -sS -X POST "$BASE/diagnose" -H 'Content-Type: application/json' -d '{
  "routes": [
    {"prefix": "10.0.0.0/8", "action": "NH1", "status": "confirmed"},
    {"prefix": "10.1.0.0/16", "action": "NH2", "status": "pending"}
  ],
  "snapshot": {
    "installed_count": 2,
    "observations": [
      {"address": "10.1.2.3", "result": "NH1"}
    ]
  }
}' | python3 -m json.tool
