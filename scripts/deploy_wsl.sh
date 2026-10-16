#!/bin/bash
# EtherealFlow · 把 WSL 流式识别服务部署到本机 r2t2 账号下。
#
# 在 Windows 上这样调用（不修改任何现有文件，只新增 /home/r2t2/etherealflow）：
#
#   wsl.exe -d Ubuntu -u r2t2 -- bash -l "/mnt/d/<...>/EtherealFlow/scripts/deploy_wsl.sh"
#
# 参数：
#   --install-service   额外安装 systemd 用户服务（开机自启需要先 enable-linger）
#   --restart           部署后重启服务
set -euo pipefail

# --------------------------------------------------------------------------- #
# 本机护栏：这个脚本是给「新机器」用的（把服务复制到 $HOME 下再注册 systemd 单元）。
# 本工作区已经自带完整运行时（workspace/runtime/），服务也是直接从工作区启动的，
# 再跑这个脚本会造出第二份副本 —— 直接拦掉。
# --------------------------------------------------------------------------- #
WORKSPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -f "$WORKSPACE_DIR/runtime/scripts/runtime.sh" ]; then
  echo "检测到工作区自带运行时：$WORKSPACE_DIR/runtime" >&2
  echo "本机请改用：bash $WORKSPACE_DIR/runtime/scripts/install_units.sh" >&2
  echo "（只注册 systemd 单元，不复制文件）" >&2
  echo "本脚本仅用于在全新机器上从零部署。" >&2
  exit 3
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${R2T2_APP_DIR:-/home/r2t2/etherealflow}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_NAME="r2t2-stream.service"

INSTALL_SERVICE=0
RESTART=0
for arg in "$@"; do
  case "$arg" in
    --install-service) INSTALL_SERVICE=1 ;;
    --restart) RESTART=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

echo "源目录 : $SRC_DIR"
echo "目标   : $TARGET"
[[ -f "$SRC_DIR/wsl/r2t2_stream_server.py" ]] || { echo "找不到 wsl/r2t2_stream_server.py" >&2; exit 1; }

mkdir -p "$TARGET/logs" "$TARGET/tools"
cp -v "$SRC_DIR/wsl/r2t2_stream_server.py" "$TARGET/"
cp -v "$SRC_DIR/scripts/start_stream_server.sh" "$TARGET/"
cp -v "$SRC_DIR/tools/test_stream_client.py" "$TARGET/tools/" 2>/dev/null || true
chmod +x "$TARGET/start_stream_server.sh"

if [[ "$INSTALL_SERVICE" == "1" ]]; then
  mkdir -p "$UNIT_DIR"
  sed "s|@APP_DIR@|$TARGET|g" "$SRC_DIR/scripts/r2t2-stream.service" > "$UNIT_DIR/$UNIT_NAME"
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT_NAME"
  echo "已安装 systemd 用户服务：$UNIT_DIR/$UNIT_NAME"
  echo "提示：要让它在未登录时也自动启动，需要（在能 sudo 的账号下执行一次）："
  echo "      sudo loginctl enable-linger $(whoami)"
fi

if [[ "$RESTART" == "1" ]]; then
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if systemctl --user list-unit-files "$UNIT_NAME" >/dev/null 2>&1 \
     && systemctl --user cat "$UNIT_NAME" >/dev/null 2>&1; then
    systemctl --user restart "$UNIT_NAME"
    sleep 1
    systemctl --user --no-pager status "$UNIT_NAME" || true
  else
    echo "未安装 systemd 服务，改为后台直接启动"
    pkill -f "r2t2_stream_server.py" 2>/dev/null || true
    sleep 1
    setsid nohup "$TARGET/start_stream_server.sh" >/dev/null 2>&1 < /dev/null &
    echo "已后台启动，日志见 $TARGET/logs/service.log"
  fi
fi

echo "部署完成。"
