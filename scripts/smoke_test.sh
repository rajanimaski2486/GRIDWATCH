#!/usr/bin/env bash
# GridWatch NAT smoke test — run before trusting any deploy.
#
#   ./scripts/smoke_test.sh
#
# Stages 1-5 are offline and free. Stage 6 is the only one that spends NIM
# credits. Reads .env automatically.

set -uo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

PY=".venv/bin/python"
NAT=".venv/bin/nat"
CONFIG="src/hackathon_nyc/configs/config_gridwatch.yml"
fail() { echo "  ✗ $1"; exit 1; }

echo "== 1. Toolchain"
[ -x "$PY" ] || fail "no .venv — run: uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ."
$PY --version | sed 's/^/  /'
$PY -c "import importlib.metadata as m; print('  nvidia-nat', m.version('nvidia-nat'))" || fail "nvidia-nat not installed"

echo "== 2. Tool discovery (entry point in pyproject.toml)"
found=$($PY - <<'EOF'
from nat.runtime.loader import PluginTypes, discover_and_register_plugins
discover_and_register_plugins(PluginTypes.ALL)
from nat.cli.type_registry import GlobalTypeRegistry
r = GlobalTypeRegistry.get()
names = []
for attr in ("_registered_function_groups", "_registered_functions"):
    for k in getattr(r, attr, {}):
        n = k.static_type() if callable(getattr(k, "static_type", None)) else None
        if n and ("nyc" in str(n) or "parallel_agent" in str(n)):
            names.append(str(n))
print(" ".join(sorted(names)))
EOF
)
[ -n "$found" ] || fail "NAT cannot see the GridWatch tools — is the package installed (-e .)?"
echo "  $found"

echo "== 3. Config validation"
# Capture first: `grep -q` closes the pipe early, SIGPIPEs nat, and trips pipefail.
validate_out=$($NAT validate --config_file "$CONFIG" 2>&1)
case "$validate_out" in
  *"is valid"*) echo "  ✓ $CONFIG" ;;
  *) echo "$validate_out" | sed 's/^/    /'; fail "config invalid" ;;
esac

echo "== 4. Policy gate (deterministic — no LLM involved)"
$PY - <<'EOF' || fail "policy gate is not enforcing"
import os
from hackathon_nyc import policy

d = policy.evaluate_mutation("delete", "")
assert not d.allowed, "BULK DELETE WAS ALLOWED"
print("  ✓ unscoped delete refused")

assert policy.evaluate_mutation("delete", "abc123").allowed
print("  ✓ scoped delete permitted")

assert not policy.is_trusted_source("citizen_sms")
assert policy.is_trusted_source("dispatcher")
print("  ✓ citizen_sms untrusted, dispatcher trusted")

os.environ["ALERTS_ENABLED"] = "false"
assert not policy._load_config()["alerts_enabled"], "kill switch ignored"
del os.environ["ALERTS_ENABLED"]
print("  ✓ ALERTS_ENABLED kill switch honored")
EOF

echo "== 5. Workflow build + tool count (offline, dummy key)"
NVIDIA_API_KEY="${NVIDIA_API_KEY:-nvapi-dummy}" $PY - "$CONFIG" <<'EOF' || fail "workflow build failed"
import asyncio, sys
from pathlib import Path
from nat.runtime.loader import PluginTypes, discover_and_register_plugins
discover_and_register_plugins(PluginTypes.ALL)
from nat.utils.io.yaml_tools import yaml_load
from nat.utils.data_models.schema_validator import validate_schema
from nat.data_models.config import Config
from nat.builder.workflow_builder import WorkflowBuilder

GROUPS = ["flood_tools", "complaint_tools", "geo_tools", "crm_tools", "history_tools"]

async def main():
    cfg = validate_schema(yaml_load(Path(sys.argv[1])), Config)
    async with WorkflowBuilder.from_config(cfg) as b:
        await b.build()
        total = 0
        for g in GROUPS:
            fns = await (await b.get_function_group(g)).get_accessible_functions()
            total += len(fns)
            print(f"    {g:16} {len(fns):2}")
        print(f"  ✓ workflow built, {total} tools resolved")
        crm = await (await b.get_function_group("crm_tools")).get_accessible_functions()
        for required in ("check_alerts", "check_mutation_allowed"):
            assert any(required in k for k in crm), f"{required} missing from crm_tools include list!"
        print("  ✓ check_alerts + check_mutation_allowed reachable by the agent")
asyncio.run(main())
EOF

echo "== 6. Live inference (NVIDIA-hosted Nemotron)"
case "${NVIDIA_API_KEY:-}" in
  ""|*paste-yours-here*|nvapi-dummy*)
    echo "  ⊘ skipped — no real NVIDIA_API_KEY yet."
    echo "    1. Get one: https://build.nvidia.com -> any Nemotron model -> 'Get API Key'"
    echo "    2. Paste it into .env (gitignored) on the NVIDIA_API_KEY= line"
    echo "    3. Re-run this script"
    exit 0 ;;
esac
$NAT run --config_file "$CONFIG" --input "What is the current date and time?" 2>&1 | tail -12
echo
echo "If stage 6 returned an answer, the NAT path works end to end."
