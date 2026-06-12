#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/mnt/f/chatbot}"
CLAWBENCH_DIR="${CLAWBENCH_DIR:-$REPO_ROOT/.benchmarks/clawbench_original_agent}"
IMAGE="${IMAGE:-clawbench-original-agent:local}"
MODEL="${MODEL:-deepseek/deepseek-chat}"
TASK="${TASK-t1-fs-quick-note}"
RUNS="${RUNS:-1}"
CONCURRENCY="${CONCURRENCY:-1}"
GATEWAY_PORT="${GATEWAY_PORT:-18789}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.deepseek.com/v1}"
SKIP_BUILD="${SKIP_BUILD:-0}"
APT_MIRROR="${APT_MIRROR:-}"
APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR:-}"
GATEWAY_WAIT_SECONDS="${GATEWAY_WAIT_SECONDS:-1200}"
BROWSER_ENABLED="${BROWSER_ENABLED:-false}"
STATE_CACHE_DIR="${STATE_CACHE_DIR:-$REPO_ROOT/.benchmarks/clawbench_original_state_cache}"
WSL_STATE_DIR="${WSL_STATE_DIR:-/root/clawbench_openclaw_state}"
PREPARE_ONLY="${PREPARE_ONLY:-0}"
CLAWBENCH_REQUEST_TIMEOUT="${CLAWBENCH_REQUEST_TIMEOUT:-240}"
CLAWBENCH_CONNECT_TIMEOUT="${CLAWBENCH_CONNECT_TIMEOUT:-180}"

get_env_value() {
  local file="$1"
  local name="$2"
  [ -f "$file" ] || return 0
  awk -F= -v key="$name" '
    $0 !~ /^[[:space:]]*#/ && $1 == key {
      sub(/^[^=]*=/, "", $0)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
      gsub(/^"|"$/, "", $0)
      gsub(/^'\''|'\''$/, "", $0)
      print $0
      exit
    }
  ' "$file"
}

DOTENV="$REPO_ROOT/.env"
DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-$(get_env_value "$DOTENV" DEEPSEEK_API_KEY)}"
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "DEEPSEEK_API_KEY was not found in $DOTENV or environment." >&2
  exit 1
fi

if [ ! -d "$CLAWBENCH_DIR" ]; then
  mkdir -p "$(dirname "$CLAWBENCH_DIR")"
  git clone --depth 1 https://github.com/openclaw/clawbench "$CLAWBENCH_DIR"
fi

if ! /usr/bin/docker info >/dev/null 2>&1; then
  if command -v dockerd >/dev/null 2>&1; then
    nohup dockerd >/var/log/dockerd.log 2>&1 &
    sleep 5
  fi
fi
/usr/bin/docker info --format 'Docker server: {{.ServerVersion}}'

if [ "$SKIP_BUILD" != "1" ]; then
  if [ -n "$APT_MIRROR" ] || [ -n "$APT_SECURITY_MIRROR" ]; then
    APT_MIRROR="${APT_MIRROR:-http://deb.debian.org/debian}"
    APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR:-http://deb.debian.org/debian-security}"
    BUILD_DOCKERFILE="/tmp/clawbench_original_agent.Dockerfile"
    awk -v mirror="$APT_MIRROR" -v security_mirror="$APT_SECURITY_MIRROR" '
      { print }
      /DEBIAN_FRONTEND=noninteractive/ {
        print "RUN sed -i \"s#http://deb.debian.org/debian-security#" security_mirror "#g; s#http://deb.debian.org/debian#" mirror "#g\" /etc/apt/sources.list.d/debian.sources"
      }
    ' "$CLAWBENCH_DIR/Dockerfile" > "$BUILD_DOCKERFILE"
    /usr/bin/docker build --no-cache -t "$IMAGE" -f "$BUILD_DOCKERFILE" "$CLAWBENCH_DIR"
  else
    /usr/bin/docker build --no-cache -t "$IMAGE" "$CLAWBENCH_DIR"
  fi
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
SAFE_MODEL="$(printf '%s' "$MODEL" | sed 's/[^A-Za-z0-9_.-]/_/g')"
SAFE_TASK="$(printf '%s' "${TASK:-all-public}" | sed 's/[^A-Za-z0-9_.-]/_/g')"
RESULT_NAME="${RESULT_NAME:-original_agent_${SAFE_MODEL}_${SAFE_TASK}_${STAMP}.json}"
RUN_ROOT="$REPO_ROOT/.benchmarks/clawbench_original_runs/$STAMP"
DATA_DIR="$RUN_ROOT/data"
mkdir -p "$DATA_DIR"
mkdir -p "$WSL_STATE_DIR"
if [ -d "$STATE_CACHE_DIR" ] && [ ! -d "$WSL_STATE_DIR/plugin-runtime-deps" ]; then
  cp -r "$STATE_CACHE_DIR"/. "$WSL_STATE_DIR"/ 2>/dev/null || true
fi

MODEL_ID="$MODEL"
MODEL_ID="${MODEL_ID#openai/}"
TOKEN="local-clawbench-token-$STAMP"
RESULT_IN_CONTAINER="/data/results/$RESULT_NAME"
SCRIPT_PATH="$RUN_ROOT/run_inside_container.sh"

cat > "$SCRIPT_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail
mkdir -p /data/results /tmp/clawbench_run_cache "\$OPENCLAW_STATE_DIR"
rm -f "\$OPENCLAW_STATE_DIR/openclaw.json.bak" "\$OPENCLAW_STATE_DIR/logs/config-health.json"
mkdir -p "\$OPENCLAW_STATE_DIR/agents/main/agent" "\$OPENCLAW_STATE_DIR/workspace"
cat > "\$OPENCLAW_STATE_DIR/openclaw.json" <<'JSON'
{
  "gateway": {
    "mode": "local",
    "bind": "loopback",
    "port": $GATEWAY_PORT,
    "auth": { "mode": "token" }
  },
  "browser": {
    "enabled": $BROWSER_ENABLED,
    "headless": true,
    "noSandbox": true,
    "ssrfPolicy": { "allowedHostnames": ["localhost", "127.0.0.1"] }
  },
  "tools": {
    "profile": "coding",
    "alsoAllow": $([ "$BROWSER_ENABLED" = "true" ] && printf '["browser"]' || printf '[]')
  },
  "agents": {
    "defaults": {
      "workspace": "/tmp/openclaw_state/workspace",
      "model": { "primary": "$MODEL" }
    }
  },
  "cron": { "enabled": false }
}
JSON

echo "OpenClaw version:"
openclaw --version || true
echo "Starting OpenClaw gateway on :$GATEWAY_PORT"
openclaw gateway run --allow-unconfigured --dev --bind loopback --port $GATEWAY_PORT --auth token --token "$TOKEN" --compact > /data/gateway.log 2>&1 &
gw_pid=\$!
for i in \$(seq 1 $GATEWAY_WAIT_SECONDS); do
  if grep -q "\\[gateway\\] ready" /data/gateway.log 2>/dev/null; then
    echo "Gateway healthy after \${i}s"
    break
  fi
  if [ "\$i" -eq $GATEWAY_WAIT_SECONDS ]; then
    echo "Gateway failed to start"
    tail -120 /data/gateway.log || true
    exit 1
  fi
  sleep 1
done

if [ "${PREPARE_ONLY:-0}" = "1" ]; then
  echo "Prepare-only mode: gateway dependency cache is ready."
  kill "\$gw_pid" 2>/dev/null || true
  wait "\$gw_pid" 2>/dev/null || true
  exit 0
fi

set +e
export PYTHONPATH="/home/node/app:\${PYTHONPATH:-}"
python -c 'import inspect, clawbench.harness; print("Harness:", clawbench.harness.__file__, "patched=", "gateway_reconnect_after_agent_create" in inspect.getsource(clawbench.harness.BenchmarkHarness))'
TASK_ARGS=()
if [ -n "$TASK" ]; then
  TASK_ARGS=(--task "$TASK")
fi
python -c 'from clawbench.cli import main; main()' run \
  --model "$MODEL" \
  --runs "$RUNS" \
  --concurrency "$CONCURRENCY" \
  "\${TASK_ARGS[@]}" \
  --no-randomize \
  --output "$RESULT_IN_CONTAINER" \
  > /data/clawbench.log 2>&1
status=\$?
set -e
kill "\$gw_pid" 2>/dev/null || true
wait "\$gw_pid" 2>/dev/null || true
tail -120 /data/clawbench.log || true
exit "\$status"
EOF
chmod +x "$SCRIPT_PATH"

/usr/bin/docker run --rm \
  -e OPENAI_API_KEY="$DEEPSEEK_API_KEY" \
  -e DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  -e OPENAI_API_BASE="$OPENAI_BASE_URL" \
  -e OPENCLAW_GATEWAY_TOKEN="$TOKEN" \
  -e OPENCLAW_GATEWAY_URL="ws://127.0.0.1:$GATEWAY_PORT" \
  -e OPENCLAW_HOME=/home/node \
  -e OPENCLAW_STATE_DIR=/tmp/openclaw_state \
  -e OPENCLAW_CONFIG_PATH=/tmp/openclaw_state/openclaw.json \
  -e CLAWBENCH_RUN_CACHE_DIR=/data/run_cache \
  -e CLAWBENCH_CONNECT_TIMEOUT="$CLAWBENCH_CONNECT_TIMEOUT" \
  -e CLAWBENCH_REQUEST_TIMEOUT="$CLAWBENCH_REQUEST_TIMEOUT" \
  -e PREPARE_ONLY="$PREPARE_ONLY" \
  -v "$DATA_DIR:/data" \
  -v "$WSL_STATE_DIR:/tmp/openclaw_state" \
  -v "$CLAWBENCH_DIR/clawbench:/home/node/app/clawbench:ro" \
  -v "$SCRIPT_PATH:/tmp/run_inside_container.sh:ro" \
  "$IMAGE" bash /tmp/run_inside_container.sh

status=$?
cat > "$RUN_ROOT/summary.json" <<EOF
{
  "status": "$(if [ "$status" -eq 0 ]; then echo finished; else echo failed; fi)",
  "exit_code": $status,
  "run_root": "$RUN_ROOT",
  "model": "$MODEL",
  "task": "${TASK:-all-public}",
  "runs": $RUNS,
  "result_json": "$DATA_DIR/results/$RESULT_NAME",
  "gateway_log": "$DATA_DIR/gateway.log",
  "clawbench_log": "$DATA_DIR/clawbench.log",
  "note": "Uses ClawBench original OpenClaw agent/harness. DeepSeek is routed through the OpenAI-compatible provider config."
}
EOF
cat "$RUN_ROOT/summary.json"
exit "$status"
