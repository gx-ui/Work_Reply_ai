#!/usr/bin/env bash
# Work Reply AI - 后端启动（单文件，置于项目根目录）
#
# 用法：
#   ./start.sh                 # 前台启动（当前终端退出即停止，适合本机调试）
#   ./start.sh --daemon        # 后台启动（nohup，断开 SSH 仍运行；日志见 log/uvicorn.log）
#   ./start.sh --stop          # 停止 --daemon 启动的进程（读取 run/work_reply_ai.pid）
#   ./start.sh --restart       # 先 --stop 再 --daemon
#   ./start.sh --install       # 创建 Conda 环境、安装依赖后**前台**启动
#   ./start.sh --install-only  # 仅创建 Conda 环境并安装依赖
#
# 配置与环境变量：
#   WORK_REPLY_PROFILE=dev   # 默认，使用 config/config.json（开发环境）
#   WORK_REPLY_PROFILE=test    # 使用 config/config_dev.json（测试环境）
#   WORK_REPLY_CONFIG_FILE=    # 显式指定配置文件路径（相对项目根或绝对路径，优先级最高）
#
# 其它可选环境变量：
#   HOST=0.0.0.0  PORT=8003  WORKERS=1  RELOAD=1（仅开发）
#   WORK_REPLY_CONDA_ENV=work_reply_ai  # Conda 环境名（默认 work_reply_ai）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

WORK_REPLY_CONDA_ENV="${WORK_REPLY_CONDA_ENV:-work_reply_ai}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8003}"
WORKERS="${WORKERS:-4}"
RELOAD="${RELOAD:-0}"
export WORK_REPLY_PROFILE="${WORK_REPLY_PROFILE:-dev}"
if [[ -n "${WORK_REPLY_CONFIG_FILE:-}" ]]; then
  export WORK_REPLY_CONFIG_FILE
fi

die() {
  echo "[start.sh] 错误: $*" >&2
  exit 1
}

resolve_config_path() {
  if [[ -n "${WORK_REPLY_CONFIG_FILE:-}" ]]; then
    local p="$WORK_REPLY_CONFIG_FILE"
    if [[ "$p" != /* ]]; then
      p="$ROOT/$p"
    fi
    printf '%s' "$p"
    return
  fi
  case "${WORK_REPLY_PROFILE}" in
    test) printf '%s' "$ROOT/config/config_dev.json" ;;
    dev|*) printf '%s' "$ROOT/config/config.json" ;;
  esac
}

ensure_config() {
  local cfg
  cfg="$(resolve_config_path)"
  [[ -f "$cfg" ]] || die "缺少配置文件: $cfg（WORK_REPLY_PROFILE=$WORK_REPLY_PROFILE）"
  echo "[start.sh] 将加载配置: $cfg"
}

# 初始化 conda（供 conda activate 使用）
init_conda() {
  if [[ "${CONDA_SH_LOADED:-0}" == "1" ]]; then
    return 0
  fi
  command -v conda >/dev/null 2>&1 || die "未找到 conda，请先安装 Miniconda/Anaconda 并确保 conda 在 PATH 中"
  local base
  base="$(conda info --base 2>/dev/null)" || die "无法获取 conda 根目录（conda info --base 失败）"
  if [[ -f "$base/etc/profile.d/conda.sh" ]]; then
    # shellcheck source=/dev/null
    source "$base/etc/profile.d/conda.sh"
  else
    die "未找到 $base/etc/profile.d/conda.sh ，无法用 bash 初始化 conda"
  fi
  export CONDA_SH_LOADED=1
}

conda_env_prefix() {
  local base
  base="$(conda info --base 2>/dev/null)" || die "无法获取 conda 根目录"
  printf '%s' "$base/envs/$WORK_REPLY_CONDA_ENV"
}

activate_conda_env() {
  init_conda
  local prefix
  prefix="$(conda_env_prefix)"
  [[ -d "$prefix" ]] || die "未找到 Conda 环境: $WORK_REPLY_CONDA_ENV（$prefix），请先执行: $0 --install-only"
  conda activate "$WORK_REPLY_CONDA_ENV"
}

install_deps() {
  init_conda
  local prefix
  prefix="$(conda_env_prefix)"
  if [[ ! -d "$prefix" ]]; then
    echo "[start.sh] 创建 Conda 环境: $WORK_REPLY_CONDA_ENV（Python 3.10）"
    conda create -n "$WORK_REPLY_CONDA_ENV" python=3.10 -y
  else
    echo "[start.sh] 已存在 Conda 环境: $WORK_REPLY_CONDA_ENV ，将安装/更新依赖"
  fi
  conda activate "$WORK_REPLY_CONDA_ENV"
  pip install --upgrade pip
  pip install -r "$ROOT/requirements.txt"
  echo "[start.sh] 依赖安装完成"
}

run_uvicorn() {
  activate_conda_env
  ensure_config

  # 刷新 Milvus 缓存
  echo "[start.sh] 刷新 Milvus 缓存..."
  if python "$ROOT/scripts/cache_file_name.py"; then
    echo "[start.sh] Milvus 缓存刷新成功"
  else
    echo "[start.sh] 警告: Milvus 缓存刷新失败，将继续启动" >&2
  fi

  local -a extra=()
  if [[ "$RELOAD" == "1" ]]; then
    extra+=(--reload)
    echo "[start.sh] 警告: 已开启 --reload，仅用于开发" >&2
  fi
  echo "[start.sh] 启动 uvicorn  host=$HOST port=$PORT workers=$WORKERS profile=$WORK_REPLY_PROFILE conda_env=$WORK_REPLY_CONDA_ENV"
  exec uvicorn app.app:app --host "$HOST" --port "$PORT" --workers "$WORKERS" "${extra[@]}"
}

# 通过 conda 环境内绝对路径调用 uvicorn，无需在 nohup 子 shell 里 conda activate
uvicorn_bin() {
  init_conda
  local prefix
  prefix="$(conda_env_prefix)"
  [[ -d "$prefix" ]] || die "未找到 Conda 环境: $WORK_REPLY_CONDA_ENV（$prefix），请先执行: $0 --install-only"
  local uv="$prefix/bin/uvicorn"
  [[ -x "$uv" ]] || die "未找到可执行文件: $uv"
  printf '%s' "$uv"
}

run_uvicorn_daemon() {
  ensure_config
  local UVICORN LOG_FILE PID_FILE oldpid
  UVICORN="$(uvicorn_bin)"
  mkdir -p "$ROOT/log" "$ROOT/run"
  LOG_FILE="$ROOT/log/uvicorn.log"
  PID_FILE="$ROOT/run/work_reply_ai.pid"

  if [[ -f "$PID_FILE" ]]; then
    oldpid="$(tr -d ' \n\r' <"$PID_FILE" || true)"
    if [[ -n "$oldpid" ]] && kill -0 "$oldpid" 2>/dev/null; then
      die "进程已在运行 pid=$oldpid（日志: $LOG_FILE）。停止请执行: $0 --stop"
    fi
    rm -f "$PID_FILE"
  fi

  # 刷新 Milvus 缓存
  echo "[start.sh] 刷新 Milvus 缓存..."
  if python "$ROOT/scripts/cache_file_name.py"; then
    echo "[start.sh] Milvus 缓存刷新成功"
  else
    echo "[start.sh] 警告: Milvus 缓存刷新失败，将继续启动" >&2
  fi

  local -a extra=()
  if [[ "$RELOAD" == "1" ]]; then
    extra+=(--reload)
    echo "[start.sh] 警告: 后台模式不建议 RELOAD=1（多 worker 时与 --reload 也可能不兼容）" >&2
  fi

  echo "[start.sh] 后台启动 uvicorn（nohup） host=$HOST port=$PORT workers=$WORKERS profile=$WORK_REPLY_PROFILE"
  echo "[start.sh] 日志: $LOG_FILE"
  echo "[start.sh] PID 文件: $PID_FILE"
  cd "$ROOT"
  # nohup 忽略 SIGHUP，SSH 断开后主进程仍保留（请勿再对终端按 Ctrl+C 杀父 shell 已无关）
  nohup "$UVICORN" app.app:app --host "$HOST" --port "$PORT" --workers "$WORKERS" "${extra[@]}" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  echo "[start.sh] 已启动 pid=$(cat "$PID_FILE")，断开 SSH 后仍可通过该 pid 或端口 $PORT 访问服务"
}

stop_uvicorn_daemon() {
  local PID_FILE
  PID_FILE="$ROOT/run/work_reply_ai.pid"
  [[ -f "$PID_FILE" ]] || die "无 PID 文件: $PID_FILE（可能未用 --daemon 启动）"
  local pid
  pid="$(tr -d ' \n\r' <"$PID_FILE" || true)"
  [[ -n "$pid" ]] || die "PID 文件为空: $PID_FILE"
  if kill -0 "$pid" 2>/dev/null; then
    echo "[start.sh] 停止 pid=$pid ..."
    kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      echo "[start.sh] 进程仍在，发送 SIGKILL ..."
      kill -KILL "$pid" 2>/dev/null || true
    fi
  else
    echo "[start.sh] 进程 $pid 已不存在，清理 PID 文件"
  fi
  rm -f "$PID_FILE"
  echo "[start.sh] 已停止"
}

mode="${1:-}"

case "$mode" in
  --install-only)
    install_deps
    ;;
  --install)
    install_deps
    run_uvicorn
    ;;
  "")
    run_uvicorn
    ;;
  --daemon|-d)
    run_uvicorn_daemon
    ;;
  --stop)
    stop_uvicorn_daemon
    ;;
  --restart)
    if [[ -f "$ROOT/run/work_reply_ai.pid" ]]; then
      stop_uvicorn_daemon || true
    fi
    run_uvicorn_daemon
    ;;
  -h|--help)
    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    die "未知参数: $mode （使用 --help 查看说明）"
    ;;
esac
