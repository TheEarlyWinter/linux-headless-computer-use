# Linux Headless Computer Use

> **告别物理抢鼠标硬控！专为 AI Agent（HanaAgent / Hanako、Claude Computer Use、自定义桌面 Agent）打造的 Linux 无感后台桌面自动化沙箱技能。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux%20(Ubuntu%2FFedora%2FArch)-orange.svg)]()
[![Pure Python](https://img.shields.io/badge/Python-3.10%2B%20(Standard%20Lib)-green.svg)]()
[![HanaAgent Skill](https://img.shields.io/badge/HanaAgent-Hanako%20Skill-purple.svg?logo=github)](https://github.com/HanaAgent)
[![Powered by OpenHanako](https://img.shields.io/badge/Powered%20by-OpenHanako-black.svg?logo=github)](https://github.com/liliMozi/openhanako)

---

## 为什么需要它？（告别 Windows 式“抢鼠标硬控”）

在传统 Windows 或普通桌面上运行 AI Computer Use 时，存在一个极其痛苦的弊端 —— **AI 会强行抢夺物理鼠标光标**：
* AI 在屏幕上移动点击，用户必须双手离开键盘，像人质一样被硬控在一旁；
* 用户只要不小心碰到鼠标或者弹出其他窗口，AI 就会发生坐标漂移当场报错崩溃；
* 在现代 Linux（Wayland）环境下，系统出于安全隔离，更是直接严禁普通客户端获取全局绝对坐标或随意静默截屏。

**本项目通过“双轨制平行运行”彻底终结了这一痛点：**
* **前台（物理屏幕）**：用户在纯血 Wayland 桌面下享受极致跟手手势、防撕裂和低功耗，鼠标完全自由，正常办公打游戏听歌；
* **后台（虚拟屏幕 `:99`）**：由 `Xvfb` 在内存中凭空开辟一块独立的 X11 显存空间。AI 在其中拥有 **100% 的绝对特权**（毫秒级静默全屏截屏、精确坐标注入、全分辨率模拟），双方互不干扰。

---

## 架构与依赖

整套工具链极致轻量（总包体小于 4MB，零冗余深度依赖）：
* **`Xvfb`**：X Virtual Framebuffer，负责在内存中虚构无头显示器（默认 `:99`）；
* **`xdotool`**：负责模拟鼠标移动、单击、双击、文本键入与功能按键；
* **`scrot`**：负责虚拟屏幕内的毫秒级无感截屏；
* **`openbox`**：轻量级窗口管理器，为虚拟屏内的应用提供真实焦点、边框与层级管理。

### Ubuntu / Debian 一键安装依赖
```bash
sudo apt update && sudo apt install -y xvfb xdotool scrot openbox
```

---

## 快速上手

### 1. 终端命令行（CLI）

内置的 `virtual_desktop.py` 纯 Python 标准库编写，零三方库依赖，开箱即用：

```bash
# 检查虚拟屏状态（不存在会自动启动）
python3 virtual_desktop.py status

# 在虚拟房间里启动目标应用（以 X11 模式运行）
python3 virtual_desktop.py launch "DISPLAY=:99 GDK_BACKEND=x11 gnome-calculator &"

# 模拟鼠标移动并点击 (X=500, Y=300)
python3 virtual_desktop.py click 500 300

# 模拟键盘打字
python3 virtual_desktop.py type "Hello AI"

# 模拟功能按键（Return, Escape, ctrl+s 等）
python3 virtual_desktop.py key Return

# 截屏并保存到指定路径
python3 virtual_desktop.py screenshot /tmp/result.png

# 任务结束关闭虚拟屏
python3 virtual_desktop.py stop
```

### 2. Python 模块级集成

直接在 Agent 代码或测试脚本中作为模块导入：

```python
import virtual_desktop as vd
import time

# 确保后台虚拟屏幕就绪（1920x1080）
vd.ensure_running(display=":99", resolution="1920x1080x24")

# 启动被测软件
pid = vd.launch("DISPLAY=:99 GDK_BACKEND=x11 gnome-text-editor &")
time.sleep(1.5)

# 点击并输入
vd.click(400, 300)
vd.type_text("自动化测试通过！")
vd.press_key("Return")

# 截取当前测试画面
shot_path = vd.screenshot("/tmp/test_report.png")
print("测试截图已生成:", shot_path)
```

---

## 作为 HanaAgent (Hanako) 技能使用

本项目原生为 **[HanaAgent (Hanako)]** 生态量身打造，内置标准规范的 `SKILL.md`。

### 安装到 Hanako
* **方式一：Git 克隆**
  ```bash
  cd ~/.hanako/skills/工具/
  git clone https://github.com/TheEarlyWinter/linux-headless-computer-use.git
  ```
* **方式二：手动放置**
  直接将本项目目录放置在 `~/.hanako/skills/工具/linux-headless-computer-use/`，HanaAgent 会自动识别并注册。

### 在 Hanako 中体验
在与 HanaAgent 会话时，直接自然语言吩咐：
> *“在后台虚拟屏里启动软件 XXX，测试点击某个按钮并截张图汇报。”*

Agent 将自动调度本技能完成全流程闭环，全程零打扰、零抢夺。

---

## 致谢

本项目依托并致敬以下开源基石与生态构建者：

* **[OpenHanako / HanaAgent](https://github.com/liliMozi/openhanako)**：由 [@liliMozi](https://github.com/liliMozi) 倾力打造的个人 AI Agent 平台（生态组织 [@HanaAgent](https://github.com/HanaAgent)）。特别致谢 liliMozi 与 OpenHanako 社区为 AI 原生交互提供的卓越架构与生态土壤。

---

## 开源许可证

本项目采用 [MIT License](LICENSE) 开源。
欢迎贡献 PR 与 Issue！
