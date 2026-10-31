# 触控板制谱器 · TouchBridge → RPE

> 用 Mac 触控板采集多根手指的位置 / 轨迹 / 手势，实时生成 **Phigros / RPE** 谱面，一键导出 `.pez`，可直接游玩，也能丢进 PhiEdit / Phira 继续编辑。

---

## ⚠️ 适配警告（重点！请先读）

- **支持 macOS 与 Windows（双平台）。** 触控板采集走两条完全不同的路：
  - **macOS**：依赖苹果未公开（private）的 `MultitouchSupport` 框架；
  - **Windows**：依赖微软的【精确式触控板 / Precision Touchpad（PTP）】HID 报告（`RegisterRawInputDevices` + `HidP_*`），**老式 PS/2 触控板、部分第三方触控板读不到多点数据，用不了**；
  - **Linux**：暂不支持。
- **本工具属于个人本机调试作品，不是工业级成品。**
- **触控板采集兼容性可能不适配所有机型 / 系统版本。** 绝大多数现代 MacBook / 精确式触控板 Windows 笔电可用，但以下情况可能出问题，且不保证能修：
  - 很老（或很新）的 macOS 版本导致私有 API 符号变动；
  - 非苹果原厂触控板（外接第三方触控板、iPad 当触控板等）；
  - Windows 上**非精确式（PTP）触控板**（老式 PS/2 协议触控板拿不到多点原始数据）；
  - 不同机型的触控板坐标系 / 采样率差异导致的手感偏差（可用网页里的「采样补偿 / 自动校准」缓解）。
- **不提供任何运行保证。** 请把它当作一个「思路 + 可用雏形」来用和改，而不是开箱即用的商业软件。

如果你在某台 Mac 上跑不起来、或采集明显不准，欢迎到 [Issues](../../issues) 反馈机型 + 系统版本 + 现象，**但我们无法承诺一定修好**——这正是下面「欢迎一起来完善」的原因。

---

## 🚀 一分钟速览（完全不懂代码也能用）

```text
macOS:   下载 ZIP → 解压 → 双击 run.command → 等它自己装好 → 打开浏览器打板 → 导出 .pez
Windows: 下载 ZIP → 解压 → 双击 run.bat    → 等它自己装好 → 打开浏览器打板 → 导出 .pez
```

- **不需要会写代码、不需要会编译**。首次双击启动脚本时它会在 macOS 上**自动编译触控板采集器**、在两边都自动装 Python 环境，全程按提示点几下就行。
- **不需要注册任何账号**；整个程序只在你自己电脑上运行（只监听 `127.0.0.1` 本机回环，**不连任何外部服务器**），你的音乐和数据不会离开这台电脑。
- 唯一的前置条件：一台带**多点触控触控板**的 Mac 或 Windows 笔电（Windows 需要「精确式触控板」）。

---

## 这是什么 / 它能干嘛

传统音游自制谱要么手摆、要么用 MIDI 键盘。这个工具换了个思路：

**把你笔记本电脑的触控板当成「多点触控打谱台」**——你用几根手指在触控板上做出 Tap / 长按 / 滑动 / 快扫 等动作，程序把每根手指的**坐标、区域、移动轨迹、手势类型**实时转成 OSC 消息，再由后端还原成音符，最后按 RPE 规范打包成 `.pez`。

- 导出后能**直接在 Phigros / Phira 里游玩**；
- 也能用 **PhiEdit（RPE 桌面版）/ Phira** 打开继续精修；
- 全部流程在你**自己这台 Mac 上本地完成**，不上传任何音频 / 数据。

### 支持的手势 → 音符类型

| 触控板动作 | 生成的音符 | RPE type |
|-----------|-----------|----------|
| 短按（落指即起，几乎没移动） | Tap（蓝键） | 1 |
| 按住不动 | Hold（长条） | 2 |
| 快速横扫 / 甩动 | Flick（红键，带方向） | 3 |
| 缓慢拖移 | Drag（黄键 + 轨迹） | 4 |

> Flick / Drag 的方向、轨迹都由程序根据手指运动自动判断，不需要你在 App 里选。

---

## 工作原理（三件套）

```
  你的手指
    │  在触控板上做动作
    ▼
┌──────────────┐   OSC (UDP, 127.0.0.1:9000)   ┌────────────────────┐   内部还原   ┌──────────────┐
│ TouchBridge  │ ───────────────────────────▶  │ 后端 (Flask+SocketIO)│ ──────────▶ │  RPE / .pez  │
│ (采集器)     │    /down /move /hold /drag     │ 手势分类 + 量化 + 导出 │            │  导出文件     │
└──────────────┘    /tap /up /count            └────────────────────┘             └──────────────┘
   macOS: Swift(.app)  Windows: Python(collector_win.py)
                                                                    │
                                                       网页前端 (浏览器)
                                                       实时预览 + 设置 + 录制 + 导出
```

- **TouchBridge（采集器）**：读取触控板原始多点触控数据，封装成 OSC 发给本机后端。
  - macOS：Swift 写的轻量 app，读取触控板用苹果私有框架 `MultitouchSupport`；
  - Windows：纯 Python（标准库 ctypes）写的采集器 `TouchBridge/collector_win.py`，读取**精确式触控板 (PTP)** 用系统公开的 Raw Input + HID 解析 API，**无需任何编译**。两个平台发出**字节级一致**的 OSC 协议，后端完全无感。
- **后端**：纯 Python（Flask + SocketIO），监听 OSC、把手势分类成音符、做节拍量化与吸附、导出 RPE/PEZ。音频由浏览器播放，后端只接收浏览器上报的播放时间当节拍时钟。后端本身是跨平台的，两个系统用同一份。
- **前端**：一个网页，负责音频播放、可视化预览、参数设置、录制控制、导出。

> 🔒 **隐私说明**：OSC 监听和 Web 服务都只绑定 `127.0.0.1`（本机回环），你的电脑之外的任何设备都连不上，数据不出本机。

---

## 环境要求

| 组件 | 要求 |
|------|------|
| 系统 | **macOS**（推荐 12+；采集器在 14 上开发验证）**或 Windows 10/11** |
| 触控板 | macOS：苹果原厂多点触控板（MacBook 内置 / Magic Trackpad）；Windows：**精确式触控板（Precision Touchpad）**——设置 → 蓝牙和其他设备 → 触控板 里显示「精确式触控板」的才算 |
| Python | 3.10 及以上（启动脚本会自动创建虚拟环境，无需你手动装） |
| Swift | 仅 macOS：命令行工具（`run.command` 会引导你安装；只用于首次编译采集器） |
| 浏览器 | 任意现代浏览器（Chrome / Safari / Edge），用于打开制谱网页 |

> macOS 不需要 Xcode 完整版，只要 Command Line Tools 即可编译采集器。
> Windows 采集器是纯 Python 实现，**不需要安装任何编译器**；连 Python 都没有的话先去 [python.org](https://www.python.org/downloads/) 装一个（安装时勾选 Add to PATH）。

---

## 快速开始

### 方式一（推荐，全程双击）：macOS `run.command` / Windows `run.bat`

1. **拿代码**
   - 有 Git：`git clone <本仓库地址>` 然后 `cd touchbridge-rpe`；
   - **没有 Git（大多数小白）**：点仓库主页绿色 **Code → Download ZIP**，解压出一个 `touchbridge-rpe` 文件夹。

2. **双击对应系统的启动脚本**：
   - **macOS**：双击 `run.command`
     - **首次**它会依次：编译触控板采集器（若系统提示安装 Xcode 命令行工具，点「安装」，约几分钟，装完**再双击一次**）→ 创建 Python 虚拟环境装依赖 → 启动服务并自动打开浏览器。
     - **之后每次**双击：清理旧实例 → 启动服务 → 打开浏览器。
   - **Windows**：双击 `run.bat`
     - **首次**它会依次：自动找到 Python（没装会提示你去 python.org 装）→ 创建虚拟环境装依赖 → 清理旧实例 → 启动后端和触控板采集器 → 自动打开浏览器。
     - **之后每次**双击：清理旧实例 → 启动 → 打开浏览器。

3. 确认采集器状态：
   - macOS：打开 **TouchBridge.app**（首次编译时已自动帮你打开过一次），确认标题栏状态是「正在发送 → 127.0.0.1:9000」；
   - Windows：**TouchBridge 采集器**窗口顶部显示「正在发送 → 127.0.0.1:9000」即为正常；
     - 若显示「未检测到精确式触控板」：你的触控板不是 PTP（或驱动问题），可退化为**鼠标模拟**（窗口左侧触控板图内按住鼠标拖动＝单指），或运行 `python TouchBridge\collector_win.py --list-devices` 排查。

4. 回到浏览器制谱网页，按「使用流程」开始。

> macOS 首次编译后若打开 TouchBridge.app 被拦截，见下方 FAQ 第 1 条。

### 方式二：命令行（手动）

**macOS：**

```bash
git clone <本仓库地址> && cd touchbridge-rpe

# 1) 编译采集器
bash TouchBridge/build.sh

# 2) 启动后端 + 网页
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r ../requirements.txt
python3 app.py        # 浏览器打开 http://127.0.0.1:8765

# 3) 打开第 1 步编好的采集器
open ../TouchBridge/TouchBridge.app
```

**Windows（PowerShell / CMD 通用）：**

```powershell
git clone <本仓库地址>; cd touchbridge-rpe

# 1) 创建虚拟环境并安装依赖（没有 python 先去 python.org 装，勾选 Add to PATH）
python -m venv backend\venv
backend\venv\Scripts\pip install -r requirements.txt

# 2) 启动后端（保持窗口开着）
backend\venv\Scripts\python backend\app.py     # 自动打开 http://127.0.0.1:8765

# 3) 新开一个窗口，启动触控板采集器（纯标准库，系统 Python 即可）
python TouchBridge\collector_win.py

# 可选：确认你的触控板能不能被采集到
python TouchBridge\collector_win.py --list-devices
```

---

## 使用流程

1. 网页里「导入音频」（选你的歌），点播放确认有声音；
2. 确认采集器已启动且状态是「正在发送 → 127.0.0.1:9000」（macOS：TouchBridge.app；Windows：TouchBridge 采集器窗口）；
3. 网页点「开始监听 OSC」，右下角能看到原始消息在滚动；
4. 点「开始录制」（音乐会从头播放），跟着音乐在触控板上打板；
5. 打完点「停止录制」；
6. 填好谱面名称 / 作曲者 / 谱面设计 / 难度，点「导出」→ 得到一个 **`.pez`**；
7. 把 `.pez` 丢进 Phigros / Phira 游玩，或丢进 PhiEdit 继续改。

> 如果觉得音符整体偏早 / 偏晚，先录一段，再点网页上的「自动校准」，会自动估算采样补偿。

---

## 关于「别人能不能跑」——这是已知最大顾虑，也是我特意这样设计的原因

**结论：发的是源码，不是预编译包，所以任何人都能在自己电脑上跑。**

具体原因：

1. **macOS 读取触控板用的是 `MultitouchSupport.framework`**，这是苹果系统自带的私有框架，**每一台 Mac 都有**，而且 `MTDeviceCreateList` / `MTRegisterContactFrameCallback` / `MTDeviceStart` 这几个符号多年来一直稳定。所以它不依赖作者这台电脑的任何环境。
2. **用 `swiftc` 源码编译，编出来自动适配你自己的芯片**：在 Apple 芯片 Mac 上编出 arm64，在 Intel Mac 上编出 x86_64——不会有「架构不对跑不起来」的问题。
3. **刻意不发预编译 `.app`**：预编译包是某一台机器的架构 + 个人签名，换台 Mac 几乎必然被 Gatekeeper 拦、或架构不匹配。源码方案从根本上避开这两点。
4. **Windows 采集器是纯 Python 源码**（`collector_win.py`），用的 Raw Input / HID 解析（`RegisterRawInputDevices` / `HidP_*`）全是系统自带公开 API，标准库 ctypes 直接调用，**不需要编译器、不需要装任何依赖**。
5. 源码里**没有任何写死作者电脑路径**的东西（只有系统框架路径），所以不存在「按某台机器环境配的」问题。
6. 对小白友好：`run.command` / `run.bat` 会把环境准备这一步自动做掉（见上文），用户全程不需要懂编译。

**仍然可能的坑（诚实说明）：**

- macOS 私有 API 在极老 / 极新的系统上可能变动 → 采集器编不过或读不到；
- 非原厂触控板（外接第三方、iPad 当板）可能读不到或多点异常；
- Windows 非 PTP 触控板（老式 PS/2）读不到多点原始数据 → 只能用「鼠标模拟」降级；
- 不同机型触控板手感 / 坐标范围略有差异 → 用网页的「量化吸附 / 采样补偿 / 自动校准」调。

这些都不是「能不能跑」的死穴，但确实可能要针对具体机型调参。**这就是为什么开头写了那么大的警告，也欢迎懂 Swift / 熟悉私有框架、懂 Windows HID 的朋友一起来修。**

---

## 常见问题 FAQ

1. **双击 `run.command` 打不开 / 提示无法验证开发者？**
   右键点 `run.command` →「打开」。还不行就终端执行 `xattr -cr run.command` 后再双击。

2. **首次运行弹窗要求安装 Xcode 命令行工具？**
   点「现在安装」，等它装完（几分钟到十几分钟，取决于网络），**再双击一次** `run.command` 即可。这是一次性的事。

3. **打开采集器后显示「读不到触控板」/「未检测到精确式触控板」？**
   - **macOS**：说明这台机器/系统的私有框架读不到（见适配警告）。先确认用的是苹果原厂触控板；外接第三方触控板 / iPad 大概率不行。仍不行请到 Issues 报机型 + macOS 版本。
   - **Windows**：先运行 `python TouchBridge\collector_win.py --list-devices`，确认列表里有没有标着「触控板（PTP）」的设备（`usage=0x0005`）。没有的话说明你的触控板不是精确式触控板（老式 PS/2 触控板拿不到多点原始数据），只能用「鼠标模拟」模式单指打板；列表里**有**但网页收不到消息的，请把该命令输出发到 Issues。

4. **采集器窗口标题写着「触控板 → Max」？**
   这是历史遗留文案（这个采集器最初是为 Max/MSP 做的），功能完全一样，不用管。欢迎提 PR 改掉。

5. **网页没声音？**
   音频是在浏览器里播放的：先「导入音频」，然后用网页上的播放器（或 ① 卡片里的播放键）点播放。录制时的节拍基准也以浏览器播放时间为准。

6. **录出来的音符整体偏早（快半拍）或偏晚？**
   这是录音链路 / 音频输出的系统性延迟。录一段后点「自动校准」，或手动调「采样补偿(ms)」：正数=音符延后，负数=提前。

7. **提示端口 9000 / 8765 被占用？**
   `run.command` / `run.bat` 每次启动都会自动清理旧实例；如果你手动起的服务：
   - macOS：`lsof -ti:9000 | xargs kill`（8765 同理）再启动；
   - Windows（管理员 PowerShell）：`Get-NetTCPConnection -LocalPort 9000,8765 | %{ Stop-Process -Id $_.OwningProcess -Force }` 再启动。

8. **Windows 版有吗？**
   **有！** 就是本仓库的 `TouchBridge/collector_win.py` + `run.bat`，双击 `run.bat` 即可。要求：Windows 10/11 + 精确式触控板（PTP）+ Python 3.10+。老式 PS/2 触控板不支持（硬件协议层面拿不到多点数据），Linux 依旧没有（欢迎提 PR）。

---

## 目录结构

```
touchbridge-rpe/
├── README.md              # 你正在看的文件
├── LICENSE                # MIT
├── AI_DISCLOSURE.md       # AI 参与声明
├── requirements.txt       # 后端 Python 依赖
├── run.command            # macOS 一键启动（双击；首次自动编译采集器 + 装环境）
├── run.bat                # Windows 一键启动（双击，内部调用 run.ps1）
├── run.ps1                # Windows 启动逻辑（找 Python / 装依赖 / 清理旧实例 / 启动）
├── TouchBridge/           # 触控板采集器
│   ├── main.swift         # macOS：采集 + OSC 发送 + GUI
│   ├── collector_win.py   # Windows：PTP 采集 + OSC 发送 + GUI（纯标准库，免编译）
│   ├── Info.plist         # macOS .app 元数据
│   └── build.sh           # macOS：编译并打包成 .app
├── backend/               # 后端（Flask + SocketIO，两平台共用）
│   ├── app.py             # 接口 / OSC 监听 / 导出
│   ├── engine.py          # 手势分类 + 量化 + RPE 构建
│   ├── config.json        # 默认配置（首次运行可改）
│   ├── test_engine.py     # 单元测试（12 项，两平台可跑）
│   └── e2e_win_test.py    # Windows 端到端自检（采集器 OSC → 后端 → 音符，见文件头注释）
└── frontend/              # 网页前端（与平台无关）
    ├── index.html         # 主页面（预览 / 设置 / 录制 / 导出）
    └── socket.io.min.js   # 本地化的 SocketIO 客户端（无需联网 CDN）
```

---

## 已知问题 / TODO（欢迎一起来完善）

这个工具离「好用」还差得远，下面这些都很需要人帮忙，也欢迎直接改：

- [ ] 采集器对**非 MacBook 原厂触控板**的兼容性没验证；
- [ ] 私有框架在**未来 macOS** 上如果符号变动，采集器需要跟进；
- [ ] Windows 采集器对不同厂商 PTP 报告格式的兼容性只在少数机型验证过（PTP 是微软强制的标准报告格式，理论上面板全兼容，但欢迎各种机型实测反馈）；
- [ ] 手势分类参数（死区 / 速度阈值）目前是经验值，缺少按机型自动标定；
- [ ] 网页上改 OSC 端口目前要重启后端才生效（配置已保存，但监听 socket 是启动时建的）；
- [ ] 没有可视化地编辑判定线事件（speed / move / rotate），目前导出的流速等是固定默认；
- [ ] 录制时**没有**做触控板锁定（之前试过一版会干扰手势识别，已移除），误触目前靠手动避免；
- [ ] 导出目前只覆盖单个难度，多难度 / 多谱面工程管理待做；
- [x] ~~没有跨平台方案~~ → **Windows 已支持**（`run.bat` + `collector_win.py`，精确式触控板）；Linux 仍空缺，欢迎提 PR。

**如果你修好了上面任何一条，或加了新功能，请直接提 Pull Request。** 哪怕只是把某台机型的踩坑写进 Issues，对其他用户也是功德一件。

---

## 免责声明

1. 本软件按 **「现状」** 提供，**不附带任何明示或暗示的保证**，包括但不限于对适用性、特定用途的适用性、不侵权、不出错的保证。
2. macOS 触控板采集依赖苹果**未公开（private）API**、Windows 触控板采集依赖**精确式触控板（PTP）**设备，使用风险均由使用者自行承担；若因系统升级导致功能失效，作者不承诺修复时效。
3. 本工具与 **Phigros / 雷亚 / 任何音游官方均无隶属或合作关系**，名称提及仅出于格式兼容目的。
4. 请仅使用你**拥有合法权利**的音乐制作谱面，遵守相关版权与平台规则；因使用本工具产生的任何版权 / 合规纠纷，作者不承担责任。
5. 作者不对因使用本软件造成的任何直接或间接损失负责。
6. 若你无法在自己的电脑上跑起来，请先看上方「关于别人能不能跑」一节与 [Issues](../../issues)；**作者无法保证能解决每一台机器的兼容问题**。

---

## AI 参与声明

本项目的**后端、前端、文档与部分工程化脚本主要由 AI（WorkBuddy 智能体）在作者指导下生成 / 辅助完成**；触控板采集器（`TouchBridge/`）的触控板读取思路参考了社区常见的 `MultitouchSupport` 私有框架用法，并针对本项目做了改写；**Windows 采集器（`collector_win.py`）与启动脚本同样主要由 AI 生成**，其中 HID 结构布局经真实设备实测反推验证。详见 [AI_DISCLOSURE.md](AI_DISCLOSURE.md)。

代码在发布前经过作者本地运行验证（含触控板采集器的 `swiftc` 编译与无界面自检、后端的 12 项单元测试与导出验证、**Windows 侧的后端单测 / 采集器 OSC 自检 / 端到端录制验证**），但**仍建议你在自己的环境里 review 后再使用**，尤其是涉及系统框架调用与本地网络监听（仅本机 127.0.0.1）的部分。

---

## 许可证

[MIT](LICENSE) —— 随便用、改、分发，只要保留版权声明即可。

---

## 联系方式 / 反馈

**所有问题、建议、机型兼容性反馈，请统一走 GitHub [Issues](../../issues)。**

（不提供私人邮件 / 即时通讯支持；公开 Issue 能让后来的用户也搜到答案。）
