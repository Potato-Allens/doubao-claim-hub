#!/usr/bin/env bash
# =============================================================================
#  豆包扫码领取 + 卡密后台 —— Linux 服务器一键部署（venv + systemd，无需 Docker）
#
#  用法（在项目根目录执行）：
#      sudo bash deploy/install.sh
#  可选环境变量：
#      APP_DIR=/root/doubao-claim      安装目录
#      PORT=8080                       监听端口
#      ADMIN_PASSWORD='...'            后台密码（不传则随机生成并打印）
#      PYPI_MIRROR=https://mirrors.aliyun.com/pypi/simple/   国内加速（留空用官方源）
#      SERVICE_NAME=doubao-claim       systemd 服务名
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/root/doubao-claim}"
PORT="${PORT:-8080}"
SERVICE_NAME="${SERVICE_NAME:-doubao-claim}"
PYPI_MIRROR="${PYPI_MIRROR-https://mirrors.aliyun.com/pypi/simple/}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" != "0" ]; then echo "请用 root / sudo 运行"; exit 1; fi

if [ -z "${ADMIN_PASSWORD:-}" ]; then
  ADMIN_PASSWORD="db$(head -c 9 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
  GENERATED=1
fi

echo "==> 1/6 安装 python3-venv"
export DEBIAN_FRONTEND=noninteractive
if ! python3 -m venv --help >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq python3-venv
fi

echo "==> 2/6 拷贝代码到 ${APP_DIR}"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"
cp -r "${SRC_DIR}/app" "${APP_DIR}/"
cp "${SRC_DIR}/requirements.txt" "${APP_DIR}/"
[ -f "${SRC_DIR}/data/devices.example.json" ] && mkdir -p "${APP_DIR}/data" && \
  cp -n "${SRC_DIR}/data/devices.example.json" "${APP_DIR}/data/" || true

echo "==> 3/6 建虚拟环境"
cd "${APP_DIR}"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip || true

echo "==> 4/6 安装依赖（镜像：${PYPI_MIRROR:-官方源}）"
if [ -n "${PYPI_MIRROR}" ]; then
  ./.venv/bin/pip install -r requirements.txt -i "${PYPI_MIRROR}" --trusted-host "$(echo "${PYPI_MIRROR}" | awk -F/ '{print $3}')"
else
  ./.venv/bin/pip install -r requirements.txt
fi

echo "==> 5/6 写 systemd 服务 /etc/systemd/system/${SERVICE_NAME}.service"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=Doubao Claim (QR gift claim + redeem-code admin)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
Environment=MODE=random
Environment=REQUIRE_CODE=1
# 每张卡密可领次数（0 = 不限次）
Environment=CLAIMS_PER_CODE=0
Environment=VERIFY_RETRY=3
Environment=AUTO_NEXT=0
Environment=MAX_SESSIONS=150
Environment=DATA_DIR=${APP_DIR}/data
Environment=ADMIN_PASSWORD=${ADMIN_PASSWORD}
# 走域名/反代时取消下面这行注释并改成你的域名
#Environment=BASE_URL=https://claim.example.com
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log --timeout-keep-alive 30
Restart=always
RestartSec=3
StandardOutput=append:${APP_DIR}/logs/app.log
StandardError=append:${APP_DIR}/logs/app.log

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null

echo "==> 6/6 启动 + 自检"
systemctl restart "${SERVICE_NAME}"
sleep 4
systemctl is-active "${SERVICE_NAME}" || { echo "启动失败，看日志：journalctl -u ${SERVICE_NAME} -n 50"; exit 1; }
curl -s -m 8 "http://127.0.0.1:${PORT}/healthz" || true
echo
echo "=================================================================="
echo " 部署完成 ✅"
echo "   领取页/兑换入口 : http://<服务器IP>:${PORT}/"
echo "   卡密后台        : http://<服务器IP>:${PORT}/admin"
echo "   后台密码        : ${ADMIN_PASSWORD}$([ "${GENERATED:-0}" = "1" ] && echo '   ← 随机生成，请保存' )"
echo "   日志            : ${APP_DIR}/logs/app.log  (journalctl -u ${SERVICE_NAME})"
echo "   别忘了          : 云厂商安全组放行 ${PORT} 端口"
echo "=================================================================="
