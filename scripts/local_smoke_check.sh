#!/usr/bin/env bash
set -euo pipefail

# Local smoke-check for frontend + backend + DB wiring.
#
# Usage:
#   scripts/local_smoke_check.sh
#   scripts/local_smoke_check.sh --base-url http://127.0.0.1:8010
#   scripts/local_smoke_check.sh --skip-submit

BASE_URL="http://127.0.0.1:8000"
DO_SUBMIT=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="${2:-}"
      shift 2
      ;;
    --skip-submit)
      DO_SUBMIT=0
      shift
      ;;
    -h|--help)
      sed -n '1,16p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

echo "== Local smoke check =="
echo "Base URL: $BASE_URL"
echo

check_json() {
  local url="$1"
  local expected_key="$2"
  local expected_value="$3"
  local body
  body="$(curl -fsS "$url")"
  python3 - <<PY
import json
obj = json.loads('''$body''')
k = "$expected_key"
want = "$expected_value"
got = str(obj.get(k))
if got != want:
    raise SystemExit(f"Expected {k}={want}, got {got}; body={obj}")
print(f"OK: {k}={got}")
PY
}

echo "[1/5] API health"
check_json "$BASE_URL/api/health" "status" "ok"

echo "[2/5] DB schema health"
check_json "$BASE_URL/api/health/db" "status" "ok"

echo "[3/5] Frontend root (/)"
curl -fsSI "$BASE_URL/" >/dev/null
echo "OK: / is reachable"

echo "[4/5] Catalog JSON"
CATALOG_BODY="$(curl -fsS "$BASE_URL/data/bhulekh_catalog.json")"
python3 - <<PY
import json
obj = json.loads('''$CATALOG_BODY''')
districts = obj.get("districts", [])
if not isinstance(districts, list) or len(districts) == 0:
    raise SystemExit("Catalog has no districts.")
print(f"OK: catalog districts={len(districts)}")
PY

echo "[5/5] Workflow route contract"
if [[ "$DO_SUBMIT" -eq 1 ]]; then
  WORKFLOW_BODY="$(curl -fsS -X POST "$BASE_URL/api/workflows/land-case-search" \
    -H "Content-Type: application/json" \
    -d '{"district_label":"पुणे","taluka_label":"हवेली","village_label":"वाघोली","survey_part1":"1530","survey_option_label":"1530/3","owner_name":"Smoke Test"}')"
  python3 - <<PY
import json
obj = json.loads('''$WORKFLOW_BODY''')
if "workflow_id" not in obj:
    raise SystemExit(f"workflow_id missing: {obj}")
if obj.get("status") not in {"pending_input", "bhulekh_running", "name_variants_ready", "ecourts_running", "done", "completed", "succeeded"}:
    raise SystemExit(f"Unexpected status: {obj.get('status')}; body={obj}")
print(f"OK: workflow accepted, id={obj['workflow_id']} status={obj.get('status')}")
PY
else
  echo "Skipped sample workflow submit (--skip-submit)."
fi

echo
echo "Smoke check passed."
