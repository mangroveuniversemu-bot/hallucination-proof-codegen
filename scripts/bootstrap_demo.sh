#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_command="${PYTHON:-python3}"
dbt_python_command="${DBT_PYTHON:-python3}"
jaffle_shop_path="${JAFFLE_SHOP_PATH:-$(cd "$repo_root/.." && pwd)/jaffle_shop_duckdb}"
jaffle_shop_commit="${JAFFLE_SHOP_COMMIT:-36bde6cba69d962b83be1d52fc65a0dce1cb4ebb}"
gms_url="${DATAHUB_GMS_URL:-http://localhost:8080}"
start_datahub=false
include_impact=false
skip_install=false
skip_ingest=false
allow_unpinned_jaffle_shop=false

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap_demo.sh [options]

Options:
  --python PATH             Python 3.12+ for this project
  --dbt-python PATH         Python 3.12+ for the pinned dbt revision
  --jaffle-shop-path PATH   External dbt checkout location
  --jaffle-shop-commit SHA  Exact external dataset commit
  --gms-url URL             Local DataHub GMS URL
  --start-datahub           Start DataHub OSS Quickstart if GMS is unavailable
  --include-impact          Add the optional LOW/MEDIUM/HIGH downstream graph
  --allow-unpinned-jaffle-shop
                            Explicitly allow a different existing dbt commit
  --skip-install            Reuse existing virtual-environment dependencies
  --skip-ingest             Skip DataHub ingestion and reuse existing metadata
  -h, --help                Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_command="$2"; shift 2 ;;
    --dbt-python) dbt_python_command="$2"; shift 2 ;;
    --jaffle-shop-path) jaffle_shop_path="$2"; shift 2 ;;
    --jaffle-shop-commit) jaffle_shop_commit="$2"; shift 2 ;;
    --gms-url) gms_url="$2"; shift 2 ;;
    --start-datahub) start_datahub=true; shift ;;
    --include-impact) include_impact=true; shift ;;
    --allow-unpinned-jaffle-shop) allow_unpinned_jaffle_shop=true; shift ;;
    --skip-install) skip_install=true; shift ;;
    --skip-ingest) skip_ingest=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

project_venv="$repo_root/.venv"
project_python="$project_venv/bin/python"
project_datahub="$project_venv/bin/datahub"
dbt_venv="$jaffle_shop_path/.venv"
dbt_python="$dbt_venv/bin/python"
dbt_executable="$dbt_venv/bin/dbt"
recipe="$repo_root/recipes/dbt_recipe.yml"

gms_healthy() {
  "$project_python" -c \
    'import sys, urllib.request; urllib.request.urlopen(sys.argv[1].rstrip("/") + "/health", timeout=3)' \
    "$gms_url" >/dev/null 2>&1
}

echo "[1/7] Preparing the locked project environment"
"$python_command" -c \
  "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ is required'"
if [[ ! -x "$project_python" ]]; then
  "$python_command" -m venv "$project_venv"
fi
if [[ "$skip_install" == false ]]; then
  "$project_python" -m pip install --require-hashes -r "$repo_root/requirements.lock"
fi
if [[ ! -f "$repo_root/.env" ]]; then
  cp "$repo_root/.env.example" "$repo_root/.env"
  echo "Created .env from .env.example; add NVIDIA_API_KEY before generation."
fi
export PATH="$project_venv/bin:$PATH"
export DATAHUB_GMS_URL="${gms_url%/}"
export DATAHUB_TELEMETRY_ENABLED=false

echo "[2/7] Checking local DataHub"
if ! gms_healthy && [[ "$start_datahub" == true ]]; then
  "$project_datahub" docker quickstart
  for _attempt in $(seq 1 60); do
    gms_healthy && break
    sleep 2
  done
fi
if ! gms_healthy; then
  echo "DataHub GMS is unavailable at $gms_url." >&2
  echo "Re-run with --start-datahub or start it separately." >&2
  exit 1
fi

echo "[3/7] Preparing dbt-labs/jaffle_shop_duckdb"
if [[ ! -d "$jaffle_shop_path/.git" ]]; then
  if [[ -e "$jaffle_shop_path" ]]; then
    echo "Path exists but is not a Git checkout: $jaffle_shop_path" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$jaffle_shop_path")"
  git clone --branch duckdb --single-branch \
    https://github.com/dbt-labs/jaffle_shop_duckdb.git \
    "$jaffle_shop_path"
  git -C "$jaffle_shop_path" checkout --detach "$jaffle_shop_commit"
fi
actual_jaffle_commit="$(git -C "$jaffle_shop_path" rev-parse HEAD)"
if [[ "$actual_jaffle_commit" != "$jaffle_shop_commit" && "$allow_unpinned_jaffle_shop" == false ]]; then
  echo "Expected jaffle_shop_duckdb commit $jaffle_shop_commit but found $actual_jaffle_commit." >&2
  echo "Use --allow-unpinned-jaffle-shop only if this is intentional." >&2
  exit 1
fi
"$dbt_python_command" -c \
  "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ is required'"
if [[ ! -x "$dbt_python" ]]; then
  "$dbt_python_command" -m venv "$dbt_venv"
fi
if [[ "$skip_install" == false ]]; then
  "$dbt_python" -m pip install -r "$jaffle_shop_path/requirements.txt"
fi

echo "[4/7] Building dbt models and documentation artifacts"
(cd "$jaffle_shop_path" && "$dbt_executable" build)
(cd "$jaffle_shop_path" && "$dbt_executable" docs generate)
export DBT_PROJECT_ROOT="$jaffle_shop_path"

echo "[5/7] Ingesting dbt metadata into DataHub"
if [[ "$skip_ingest" == false ]]; then
  "$project_datahub" ingest run -c "$recipe"
fi

echo "[6/7] Building and governing the DataHub context"
(cd "$repo_root" && "$project_python" src/context_builder.py customers)
(cd "$repo_root" && "$project_python" src/bootstrap_governance.py)

echo "[7/7] Optional downstream impact graph"
if [[ "$include_impact" == true ]]; then
  (cd "$repo_root" && "$project_python" src/bootstrap_impact_demo.py)
fi

echo "Bootstrap complete. Next:"
echo "  $project_python src/orchestrator.py run --require-clean-git"
echo "Add --writeback only when you intentionally want to update DataHub."
