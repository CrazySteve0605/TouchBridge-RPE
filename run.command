#!/bin/bash
# TouchBridge -> RPE 一键启动（macOS）
# 双击本文件即可，它会按顺序自动完成：
#   0) 首次运行：自动编译触控板采集器 TouchBridge（不需要你懂任何编译知识）
#   1) 创建 Python 虚拟环境并安装依赖（首次稍慢）
#   2) 清理占用端口的旧实例
#   3) 后台启动服务，并在浏览器打开制谱网页
cd "$(dirname "$0")"

# ---- 0) 首次运行：自动编译触控板采集器 ----
if [ ! -d "TouchBridge/TouchBridge.app" ]; then
  echo "首次运行：正在编译触控板采集器 TouchBridge …"
  if ! command -v swiftc >/dev/null 2>&1; then
    echo "未检测到 Swift 编译器，需要先安装 Xcode 命令行工具（约几分钟）。"
    osascript -e 'display dialog "首次使用需要安装 Xcode 命令行工具（约几分钟）。\n点「现在安装」后稍等，装好请再双击一次本文件。" buttons {"现在安装"} default button 1' >/dev/null 2>&1 || true
    xcode-select --install
    exit 1
  fi
  if (cd TouchBridge && bash build.sh); then
    echo "✔ 采集器编译完成，已为你打开。"
    osascript -e 'display dialog "触控板采集器已编译完成并为你打开。\n\n以后制谱时：先打开 TouchBridge.app，\n再回到网页点「开始监听 OSC」即可。" buttons {"好"} default button 1' >/dev/null 2>&1 || true
    open "TouchBridge/TouchBridge.app" || true
  else
    osascript -e 'display dialog "触控板采集器编译失败。\n请把终端窗口里的报错截图发到 GitHub Issues。" buttons {"好"} default button 1'
    exit 1
  fi
fi

cd backend

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
