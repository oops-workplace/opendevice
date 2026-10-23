<div align="center">

# opendevice

[English](https://github.com/oops-workplace/opendevice/blob/master/README.md) · 简体中文

[![PyPI version](https://img.shields.io/pypi/v/opendevice.svg)](https://pypi.org/project/opendevice/)
[![PyPI pyversions](https://img.shields.io/pypi/pyversions/opendevice.svg)](https://pypi.org/project/opendevice/)
[![License: MIT](https://img.shields.io/badge/License-MIT-brightgreen.svg)](https://opensource.org/licenses/MIT)

</div>

**轻量级**的 iOS 设备交互框架：原生画面投屏、触摸与键盘输入、带坐标的控件树 ——
**不安装 WDA 也能做 UI 自动化**。

> **仅适配 iOS 27，谨慎使用。** 其他版本能否工作取决于协议，未做验证。

> **控件覆盖取决于 App 实现。** 部分 App 自绘界面、不向辅助功能树暴露内容，这类控件无法读取。

## 目录

- [演示](#演示)
- [安装](#安装)
- [命令](#命令)
  - [设备](#设备) · [界面](#界面) · [画面](#画面) · [应用与文件](#应用与文件) · [无线](#无线) · [系统](#系统)
- [作为库使用](#作为库使用)

---

## 演示

<img src="https://raw.githubusercontent.com/oops-workplace/opendevice/master/examples/webui/screenshots/demo.png" alt="webui 演示" width="880">

一个单页浏览器界面，把下列能力组合起来：实时画面、触摸、控件树坐标、硬件按键、
中文输入、USB↔Wi-Fi 热切换。该示例仅存在于 GitHub 仓库，不随 PyPI 分发包发布：

```bash
git clone https://github.com/oops-workplace/opendevice
cd opendevice
python examples/webui/run.py -d <udid> --port 8800
```

随后打开 <http://127.0.0.1:8800/>。USB 模式无需安装任何依赖，直接使用仓库内的
opendevice；Wi-Fi 模式（`--tunnel network`）需先安装 `opendevice[wifi]`，并使用
Python 3.13+。

使用前有两个前置条件：**开发者模式已开启**、**个性化镜像已挂载**。缺少任一项，
画面、触摸与控件树都不可用。两者都可以用命令完成：

```bash
opendevice devmode enable    # 开启开发者模式（按提示重启设备，重启后自动确认）
opendevice mounter mount     # 挂载个性化镜像（连接时若未挂载通常会自动补挂）
```

用 `opendevice devmode status` 和 `opendevice mounter status` 可以确认当前状态。

`webui` **不是 opendevice 的命令** —— 它是该示例自己的入口（`run.py`），参数自成
一套（`--port` / `--bind` / `--tunnel` / `--dump-h265` …），用 `--help` 查看。

---

## 安装

```bash
pip install opendevice              # 有线模式（USB，零第三方依赖）
pip install "opendevice[wifi]"      # 追加无线模式（Wi-Fi，bonjour + RemotePairing）
```

环境要求：

* **有线模式（USB）**：Python **3.9+**，主机侧只需 iTunes。
* **无线模式（Wi-Fi）**：Python **3.13+**，以及 `opendevice[wifi]` 中的依赖。
* 两种模式**能力完全一致**，无线只是更换链路。**屏幕镜像与触摸回注需要 iOS 27**
  （唯一受版本限制的部分）；其余功能（lockdown / AFC / 装包 / 应用 / 诊断 / 日志 /
  截图 / 开发者模式）按协议在更低版本上同样可用。

---

## 命令

安装后直接使用 `opendevice`；未安装时 `python -m opendevice` 等价。
`opendevice --help` 显示分组速查，`opendevice <命令> --help` 显示该命令的全部参数。

**27 条命令，按域划分**（`--help` 末尾的速查表与此一致）：

| 域 | 命令 |
|---|---|
| 设备 | `devices` `deviceinfo` |
| 界面 | `uitree` `click` `swipe` `key` `type` `hid` `sendtext` `paste` |
| 应用与文件 | `applist` `install` `uninstall` `launch` `kill` `ps` `afc` `screenshot` |
| 画面 | `stream` |
| 无线 | `pairing` `unpairing` `wifi` |
| 系统 | `devmode` `power` `syslog` `mounter` `paths` |

### 全局参数

| 参数 | 说明 |
|---|---|
| `-d/--device` | 目标设备：完整 UDID 或唯一前缀 |
| `--link auto\|usb\|wifi` | 链路：`auto`（默认）/ `usb` / `wifi` |
| `--force` | 设备被其他进程占用（投屏 / 收日志）时仍强制接管 |
| `--mux HOST:PORT` | usbmux 地址，Windows 默认 `127.0.0.1:27015` |
| `-v/--verbose`、`--log`、`--log-file` | `-v` 输出 INFO，`-vv` 输出每条报文；`--log` 写入日志文件 |

`-d` 置于命令前后均可。省略时若仅连接一台设备则直接使用，多台则报错并列出候选；
两条链路上语义一致。

`--link auto` 为 USB 优先：未指定 `-d` 且未发现任何设备时才回退 Wi-Fi。`usb` / `wifi`
是强制要求，链路不可用时直接报错，不会静默切换。

### 设备

```bash
opendevice devices                  # 列出设备（备注列显示占用者）
opendevice devices --json --fast    # JSON；--fast 只查询 usbmux，不补问机型与系统
opendevice deviceinfo               # 设备基本信息（常用字段）
opendevice deviceinfo --all         # lockdown 上的全部键（--json 输出 JSON）
opendevice deviceinfo --screen      # 主屏分辨率（像素 / 点 / 刷新率 / 方向）
```

### 界面

控件树与输入：

```bash
opendevice uitree --limit 20        # 当前界面的控件树（带点坐标，可直接用于 click）
opendevice uitree --filter 搜索     # 仅显示文字包含该片段的控件，保留其祖先
opendevice uitree --json            # 每个控件的原始字段
opendevice uitree --tap 搜索        # 定位 → 点击中心 → 重新抓取并对比
opendevice uitree --home            # 先按 Home 键回到主屏再抓取

opendevice click 200,300            # 点击：控件树的点坐标（内部按屏幕归一化，0.5,0.5 亦可）
opendevice click 搜索               # 或指定文字：定位并点击其中心（--exact / --no-verify）
opendevice swipe 200,600 200,200    # 滑动：起点 → 终点（同样接受点坐标或 0..1）
opendevice key home                 # 硬件键：home / power / volumeUp / volumeDown / mute / siri
opendevice type "abc"               # 逐字符输入（仅 ASCII；中文请用 sendtext）
opendevice hid                      # 该设备注册的 HID surface（--raw 输出原始 JSON）
opendevice paste --set "文本"       # 设备剪贴板（--get 读回；--name 指定剪贴板名）
opendevice sendtext "中文输入测试"   # 剪贴板 + Cmd+V（--no-paste / --no-approve）
```

触摸与键盘同媒体流共用一次认证：流未开启时，命令会先建立一条流再发送。

### 画面

```bash
opendevice stream --seconds 10 --out screen.h265    # 采集 10 秒
```

采集设备输出的 H.265 Annex-B 并原样写盘（mpv / VLC 可直接播放），不做转封装。省略
`--out` 时只输出统计；指定后写入该路径（相对路径以当前目录为基准，完成后打印绝对路径）。

```
隧道 设备 fde4:7625:93a3::1 <-> 主机 fde4:7625:93a3::2  mtu=16000  RSD 端口 49814   <- 隧道信息（stderr）
视频协商：1216x2624 @ 60fps（RTP payload type 100）                               <- 设备输出规格

  时长             10.0 秒                    <- 以下为结果（stdout）
  RTP 包           2797 个 / 3.1 MB
  丢包 / 乱序       0 / 0
  滤除             10 个（非视频包：音频 / 控制）
  访问单元         564 个（关键帧 1）          <- Annex-B 中的访问单元数量
  首包是关键帧      是                         <- 否 = 开头几帧无法解码，需提前开始采集
  已写入           <输出目录>/screen.h265（3.1 MB）

可用 VLC 播放：<输出目录>/screen.h265
```

### 应用与文件

```bash
opendevice applist                  # 自行安装的应用（--system / --all / --json）
opendevice install MyApp.ipa        # 安装（自动上传至 /PublicStaging；--developer / --upgrade）
opendevice uninstall com.example.app
opendevice launch com.example.app   # 启动（--arg=-x 传参；--keep-existing 保留已有实例）
opendevice ps                       # 运行中的应用进程（--all / --front / --json）
opendevice kill com.example.app     # 终止（bundle id / 可执行文件名 / pid 均可）
opendevice kill 1234 --signal 15    # 按 pid 发送 SIGTERM（默认 9 = SIGKILL）
opendevice screenshot --out s.png   # 截取单张静态图

opendevice afc ls /                 # 设备文件系统（DCIM / Books / PhotoData …）
opendevice afc ls / --app com.x     # 进入 App 沙盒（house_arrest）：Documents / Library / tmp
opendevice afc pull /DCIM/100APPLE/IMG_0001.HEIC ./     # 设备 → 主机（目录递归）
opendevice afc pull /Documents/a.txt ./ --documents     # 配合 --app，仅取其 Documents
opendevice afc push ./a.txt /PublicStaging/a.txt        # 主机 → 设备
opendevice afc rm /PublicStaging/a.txt                  # 删除单个文件
opendevice afc rm /DCIM/100APPLE -r                     # 连同内容一起删除（目录需 -r）
```

`afc` 的四个动作为 `ls` / `pull`（设备到主机）/ `push`（主机到设备）/ `rm`，
与 Python API 的 `Afc.listdir` / `pull` / `push` / `remove` 一一对应。

`--app` 通过 house_arrest 实现：仅对开发者签名（含 get-task-allow）的 App 开放，
App Store 安装的应用一律被拒（报 `InstallationLookupFailed`）；`--documents`
还要求该 App 声明了 `UIFileSharingEnabled`。

### 无线

```bash
opendevice pairing                  # 一次性操作：在 USB 上建立远程配对（--force 重建）
opendevice unpairing                # 反向操作：删除本地记录（--all；多台设备用 -d 指定）
opendevice wifi --list              # 局域网中广播的设备
opendevice wifi --seconds 8         # 完整 Wi-Fi 链路（0 = 只建隧道，不采集流）
opendevice wifi --host 192.168.1.5  # 指定地址；省略则通过 bonjour 发现
opendevice wifi --identifier UUID   # 多台设备时按 bonjour 广播的 identifier 指定
```

### 系统

```bash
opendevice devmode status           # 亦可 reveal / enable / accept
opendevice power info               # 全部诊断信息（--json 输出 JSON）
opendevice power ioreg --class X    # IORegistry（可用 --name / --plane 收窄范围）
opendevice power battery            # 电池与供电
opendevice power restart            # 重启设备
opendevice power shutdown           # 关机
opendevice power sleep              # 息屏
opendevice syslog --seconds 10      # 系统日志（--filter 过滤；长期任务，会占用设备）

opendevice mounter list             # 镜像条目（已挂载的那条即个性化 DDI）
opendevice mounter images           # 缓存中的镜像，以及哪些适用于当前设备
opendevice mounter status           # 开发者模式是否已开启
opendevice mounter lookup --type Personalized   # 该类型的签名（--type Developer 为旧式）
opendevice mounter nonce            # 个性化镜像所需的设备 nonce
opendevice mounter fetch <build>    # 从镜像源获取并写入缓存（默认源：opendevice-ddi）
opendevice mounter unmount          # 卸载镜像（--path 指定挂载点）
opendevice mounter mount            # 手动挂载（连接时若未挂载会自动补挂）

opendevice paths                    # 数据目录位置（--json 输出 JSON）
```

开发者镜像默认自动拉取：缓存里有就不联网，
没有时 `mounter mount` 和连接时的补挂会先下一份再挂（`--url` / `OPENDEVICE_DDI_URL` 换源，`--no-fetch` 只读缓存）。

`stream` 与 `syslog` 是**长期任务**：会占用该设备（跨进程锁），同一设备上的第二个
长期任务会被阻止（`--force` 可强制接管）；`deviceinfo` / `applist` / `afc` /
`screenshot` 这类短命令照常并行执行。

---

## 作为库使用

顶层入口是 `DeviceSession`，一个对象代表一台设备：

```python
from opendevice.session import DeviceSession

with DeviceSession() as device:                   # 不指定 udid：仅连接一台设备时使用它
    device.start_stream(on_access_unit=lambda au, key: None)
    device.tap(0.5, 0.5)                          # 归一化坐标
    device.press_button("home")
    device.send_text("中文输入测试")               # 剪贴板 + Cmd+V
    for element in device.elements():
        print(element.text, element.center)       # AXElement：text / role / frame / center

    device.install("MyApp.ipa", on_progress=lambda p, s: print(p, s))
    print([app["CFBundleIdentifier"] for app in device.apps()])
    device.launch("com.example.app")
    device.screenshot("shot.png")                 # 静态图，无需开启流
    device.syslog(seconds=5, on_line=print)
    with device.afc() as files:                   # 文件系统
        files.pull("/Documents/db.sqlite", "./db.sqlite")
    device.restart()
```

需要直接访问协议层时：

```python
from opendevice import LockdownClient
from opendevice.remote.coredevice import CoreDeviceClient
from opendevice.remote.tunnel import open_rsd

with LockdownClient() as lockdown:                # 自动 StartSession（含 TLS）
    print(lockdown.get_value(None, "DeviceName")) # get_value(域, 键)；单参数视为「域」
    print(lockdown.all_values()["ProductVersion"])

handle = open_rsd()                                # 一次取得隧道与 RSD，关闭顺序由它负责
print(len(handle.rsd.services), "个服务")

with CoreDeviceClient(handle.rsd, "com.apple.coredevice.deviceinfo") as core:
    print(core.get_device_info())
handle.close()
```

Wi-Fi 链路只更换隧道，`RSD` 以上完全一致：

```python
from opendevice.remote.tunnel import open_rsd

handle = open_rsd(backend="network")               # 等价于 open_wifi_rsd()，附带收尾句柄
```
