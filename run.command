#!/bin/bash
# TouchBridge -> RPE 一键启动（macOS）
# 双击本文件即可：建虚拟环境、装依赖、杀掉旧实例、后台启动服务并在浏览器打开。
cd "$(dirname "$0")/backend"

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "未检测到 python3，请先安装 Python 3.10+" buttons {"好"} default button 1'
  exit 1
fi

if [ ! -d "venv" ]; then
  echo "创建虚拟环境…"
  python3 -m venv venv || { osascript -e 'display dialog "创建虚拟环境失败" buttons {"好"} default button 1'; exit 1; }
fi
source venv/bin/activate
echo "安装依赖（首次稍慢）…"
pip install -r ../requirements.txt -q || { osascript -e 'display dialog "依赖安装失败，请检查网络" buttons {"好"} default button 1'; exit 1; }

# 杀掉占用 8765（Web）与 9000（OSC）的旧 python 实例，避免端口冲突
echo "清理旧实例…"
for p in 8765 9000; do
  pids=$(lsof -ti tcp:$p 2>/dev/null)
  [ -n "$pids" ] && kill $pids 2>/dev/null
done
# 也清掉上一回启动遗留的 app.py 进程
pkill -f "backend/app.py" 2>/dev/null
sleep 1

echo "启动服务（后台）…"
nohup python3 app.py > /tmp/touchbridge_rpe.log 2>&1 &
echo $! > /tmp/touchbridge_rpe.pid
disown

# 等端口就绪再开浏览器
WEB_PORT=8765
for i in $(seq 1 30); do
  if curl -s -o /dev/null http://127.0.0.1:$WEB_PORT/; then
    open "http://127.0.0.1:$WEB_PORT/"
    echo "已打开浏览器：$WEB_PORT"
    exit 0
  fi
  sleep 0.5
done

osascript -e 'display dialog "服务启动超时，请查看 /tmp/touchbridge_rpe.log" buttons {"好"} default button 1'
open "/tmp/touchbridge_rpe.log"
