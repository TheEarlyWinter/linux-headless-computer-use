---
name: linux-headless-computer-use
description: Linux 无头虚拟桌面（Headless Xvfb + xdotool + scrot）与静默 Computer Use 自动化技能。专为 HanaAgent (Hanako, https://github.com/HanaAgent) 生态设计。在不占用物理屏幕、不打扰用户当前工作的前提下，在后台内存开辟独立 X11 虚拟显示器（默认 :99），启动图形软件（浏览器、Hanako、桌面应用），执行高保真模拟点击、键盘输入、窗口控制与毫秒级截屏。支持多实例并发测试、GUI 自动化与端到端质检。当用户需要在 Linux 下进行后台 GUI 测试、无感操作桌面软件、模拟点击、后台截屏、测试 Hanako 插件或应用、或者进行免硬控 Computer Use 时使用。
---

# Linux Headless Computer Use — 无感后台桌面自动化技能

## 概述
本 Skill 为 [HanaAgent (Hanako)](https://github.com/HanaAgent) 赋予在 Linux（特别是现代 Wayland 环境）下进行**完全无感、不抢鼠标、独立沙箱化**的真实桌面 GUI 自动化操作能力。

### 为什么采用虚拟显示器架构？
1. **彻底终结“抢鼠标硬控”**：在 Windows 上，传统 Computer Use 必须直接抢夺物理光标，导致用户无法操作电脑；而在本架构下，AI 在后台独立的 X11 虚拟显存中（默认 `:99`）操作，物理屏幕与鼠标 100% 自由。
2. **完美规避 Wayland 安全锁死**：Wayland 默认禁止任何客户端软件获取全局绝对坐标或随意截屏；而在后台拉起独立的 X11 虚拟屏，AI 拥有 100% 的绝对特权（无感全屏截屏、精确坐标注入、无权限弹窗）。

---

## 核心环境与依赖
系统已预装以下轻量级三件套（总大小 < 4MB）：
- **`Xvfb`**：X Virtual Framebuffer，在内存中虚拟出指定分辨率的无头 X11 屏幕。
- **`xdotool`**：负责模拟鼠标移动、单击、双击、键盘输入与窗口控制。
- **`scrot`**：负责在虚拟屏幕内毫秒级静默截屏。
- **`openbox`**：轻量级窗口管理器，负责虚拟屏内的焦点激活与标准边框。

---

## 快速调用方式

本技能已内置开箱即用的自动化辅助脚本：
`~/.hanako/skills/工具/linux-headless-computer-use/virtual_desktop.py`

### 1. 终端命令行（CLI）快速调用
Agent 可以直接通过终端命令执行全套原子操作：

```bash
VD=~/.hanako/skills/工具/linux-headless-computer-use/virtual_desktop.py

# 1. 检查或启动虚拟显示器 :99
python3 "$VD" status
python3 "$VD" start

# 2. 在虚拟屏中启动软件（例如独立测试配置的 Hanako 或文本编辑器）
python3 "$VD" launch "DISPLAY=:99 GDK_BACKEND=x11 gnome-text-editor &"
# 启动隔离 profile 的测试版 Hanako
python3 "$VD" launch "DISPLAY=:99 GDK_BACKEND=x11 /opt/HanaAgent/hanako --user-data-dir=/tmp/hanako-test-profile &"

# 3. 截屏并保存
python3 "$VD" screenshot /tmp/test_screen.png

# 4. 模拟鼠标点击目标坐标 (x, y)
python3 "$VD" click 640 400

# 5. 模拟键盘输入文字
python3 "$VD" type "Hello OpenHanako"

# 6. 按下回车或功能键
python3 "$VD" key Return
python3 "$VD" key ctrl+s

# 7. 测试完毕按目标 display 精确关闭虚拟屏（如需常驻可不执行）
python3 "$VD" stop --display :99
# --display 也可以放在子命令前：python3 "$VD" --display :99 stop

# 8. 启动本地实时网页监视器（支持 1080P 60FPS 电竞级串流）
python3 "$VD" live --port 9999 --display :99
```

---

### 2. Python 模块级集成
在复杂多步自动化测试脚本中，可直接作为 Python 模块导入：

```python
import sys
sys.path.insert(0, '/home/theearlywinter/.hanako/skills/工具/linux-headless-computer-use')
import virtual_desktop as vd
import time

# 确保环境就绪
vd.ensure_running(display=":99", resolution="1920x1080x24")

# 启动被测软件
pid = vd.launch("DISPLAY=:99 GDK_BACKEND=x11 gnome-calculator &")
time.sleep(1.5)

# 点击与输入
vd.type_text("1024*2")
vd.press_key("Return")

# 截屏留存
shot = vd.screenshot("/tmp/calc_result.png")
print("测试截图已生成:", shot)
```

---

## 典型应用场景

### 场景一：测试 Hanako 自身插件或 App
- **白盒优先**：若卡片已在当前会话中渲染，优先使用 Hanako 平台的原生 `ui_inspect` 与 `ui_action`，直接通过组件 ID 交互，零误差。
- **黑盒压测**：若需测试整个 Electron 窗口生命周期、托盘联动或崩溃防御，在 `:99` 启动隔离实例：
  ```bash
  DISPLAY=:99 GDK_BACKEND=x11 /opt/HanaAgent/hanako --user-data-dir=/tmp/test-env
  ```
  随后用 `virtual_desktop.py` 模拟真人疯狂乱点，抓取 crash 日志并输出图文质检报告。

### 场景二：桌面外部软件静默操作
当用户需要 AI 操作某款 Linux 原生图形软件（如播放器、GIMP、CAD 工具、系统设置）：
1. 悄悄在 `:99` 中拉起该程序；
2. 循环执行：`screenshot` → 视觉分析定位 → `click` / `type`；
3. 完成后将产物文件（或截图）呈现给用户，全程对用户的物理屏幕零打扰。
