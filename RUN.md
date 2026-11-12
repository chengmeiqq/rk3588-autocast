# Airtest on the RK3588 board

- Version: 1.4.3 (master @ v1.4.3)
- Python: venv `.venv` on system Python 3.8 (system Python untouched)
- Install: `.venv/bin/pip install -e .` (Tsinghua mirror)

## Board-specific fix

The adb binary shipped inside `airtest/core/android/static/adb/linux/adb` is **x86-64**, and
`linux_arm/adb` is 32-bit ARM needing an armhf loader that is not installed. Without help, Airtest
fails with `IndexError: ADB devices not found` even when a phone is plugged in.

Fix: use the system arm64 adb and export `ANDROID_HOME`:

```sh
source env.sh          # exports ANDROID_HOME=/usr/lib/android-sdk
```

## Usage

```sh
source env.sh
.venv/bin/airtest version
.venv/bin/airtest info script.air
.venv/bin/airtest run script.air --device Android:/// --log logs/run1
.venv/bin/airtest report script.air --log_root logs/run1 --outfile report.html --lang zh
```

Script template:

```python
# -*- coding: utf-8 -*-
from airtest.core.api import *
auto_setup(__file__)
touch(Template("tpl.png", threshold=0.8))
swipe((500, 1800), (500, 800))
snapshot(filename="shot.png")
```

## Verified

- `import airtest` + android backend import OK
- CLI runs a `.air` script end-to-end (offline CV: template match confidence 1.0)
- `tests/test_aircv.py`: 8 passed. `test_find_surf` fails (SURF is patented, excluded from
  opencv-contrib wheels) and `test_find_orb` misses at threshold 0.7 (confidence 0.635) — both are
  upstream limits of the prebuilt OpenCV, not install problems. Template matching (the default in
  `touch(Template(...))`) works.
- Device path: with `ANDROID_HOME` set, Airtest reaches the system adb and reports
  `ADB devices not found` when no phone is attached (correct behaviour).

## Live device run (verified 2026-11-12 11:11 UTC)

Phone: Xiaomi 12S Ultra (`892e20c9`, model 2203121C, Android 13, 1080x2400).

```sh
cd /root/.openclaw/workspace/Airtest && source env.sh
.venv/bin/airtest run realtest.air --device "Android:///892e20c9" --log logs/run3
.venv/bin/airtest report realtest.air --log_root logs/run3 --outfile logs/report.html --lang zh
```

Result: `OK`, template match on a live screenshot at confidence **1.0**, swipe changed the screen
(mean abs pixel diff 58.8). Report: `logs/report.html`.

### Snapshot path gotcha

`snapshot(filename="home.png")` writes into `Settings.LOG_DIR` (the `--log` dir), **not** the script
directory. Build the path with `os.path.join(ST.LOG_DIR, "home.png")` or the next `Image.open()`
raises `FileNotFoundError`.

### Phone prep that was needed

The phone first enumerated as `05c6:f000` with configuration `msc` (mass storage only, no ADB
interface) — `adb devices` was empty even though `lsusb` showed it. Switching the USB mode to
MTP + enabling USB debugging made it appear as `18d1:4ee7` / `892e20c9 device`.

### Known non-blocking warnings on this phone

- `Minicap setup up failed` / `Javacap setup up failed` (`INSTALL_FAILED_USER_RESTRICTED`): MIUI
  blocks the background install of Airtest's screen-capture helper. Airtest falls back to
  `adbcap` and warns the efficiency may be low — screenshots still work correctly.
  Fix if you want fast capture: enable 开发者选项 → USB 调试(安全设置) / 允许通过 USB 安装应用,
  or install `airtest/core/android/static/apks/` minicap/javacap manually.
- `adb` bundled with the package is x86-64 (see above) — always `source env.sh` first.

## 网页版：上传/框选图片 → 点击（airweb）

```sh
cd /root/.openclaw/workspace/Airtest && ./start-web.sh      # 默认 8123
# 浏览器打开 http://192.168.1.83:8123
```

界面：
1. 左侧是手机实时画面（1.2s 自动刷新）
2. **在画面上拖一个框** → 输入名字 → "存为模板"。模板存在 `templates/`
3. 模板列表里点 **"找图并点"**：在整屏搜索该模板并点它的中心；阈值可调，
   勾 "等它出现（10s）" 就是等到出现再点
4. 在画面上**单击** = 点那个坐标
5. 上滑/下滑/回桌面/返回/启动 App/输入文字 都有按钮
6. 也能直接上传一张现成的 PNG 当模板

API（可直接脚本调用，全部返回 JSON）：

| 方法 | 说明 |
|---|---|
| `GET /screen.jpg?fresh=1` | 当前画面 JPEG |
| `POST /crop` `{name,fx1,fy1,fx2,fy2}` | 框选存模板（比例坐标 0~1） |
| `POST /tap` `{fx,fy}` | 点相对位置 |
| `POST /tap_template` `{name,threshold,timeout}` | 找图并点 |
| `POST /find` `{name,threshold}` | 只找不点 |
| `POST /swipe` `{dir}` 或 `{fx1,fy1,fx2,fy2}` | 滑动 |
| `POST /key` `{name}` | home/back/enter/... |
| `POST /open` `{pkg}` | 启动 App |
| `POST /text` `{text}` | 输入文字 |
| `POST /upload` (multipart) | 上传模板图 |

实测响应：`/tap` 31ms，`/tap_template` 824ms（含一次截图+匹配），`/screen.jpg` 交换 200~220KB。

## 已做的加速（重要）

手工把 `minicap` / `minitouch` 推到 `/data/local/tmp` 后，`airlib` 默认走 MINICAP+MINITOUCH：

```sh
adb push airtest/core/android/static/stf_libs/arm64-v8a/minicap /data/local/tmp/
adb push airtest/core/android/static/stf_libs/minicap-shared/aosp/libs/android-33/arm64-v8a/minicap.so /data/local/tmp/
adb push airtest/core/android/static/stf_libs/arm64-v8a/minitouch /data/local/tmp/
adb shell chmod 755 /data/local/tmp/minicap /data/local/tmp/minitouch
```

效果：截图 0.56s → **0.09s**（6x），点击 1.6s → **0.031s**（50x）。
要退回纯 adb：`AIR_FAST=0`。

## 无线调试（root 已开）

```sh
adb shell "su -c 'setprop service.adb.tcp.port 5555; stop adbd; start adbd'"
adb connect 192.168.1.119:5555
```
实测不插线可直接截图/点击。注意手机重启后要重新执行一次。

## 流程 + 逐步验证（核心功能）

手机上的操作可以编成"流程"，**每一步做完立刻核对**，不通过就停。

网页第 4 块「流程」：
1. 选类型 → 「加一步」
   - `启动 App`：填包名，自动用包名做验证
   - `点模板`：填模板文件名，再填"点完期望出现哪个模板"
   - `点坐标 / 按键 / 输入文字 / 滑动 / 等待`
2. 「保存」存到 `flows/xxx.json`
3. 「▶ 运行并逐步验证」直接跑

### 三种验证手段

| 手段 | 怎么用 | 适合 |
|---|---|---|
| 前台 App | `expect_app: "com.larus.nova"` | 启动 App、跳转页面 |
| 屏幕文字 | `expect_text: "发送"` | 有文本的界面 |
| **出现某模板** | `expect_template: "开启投屏.png"` | **图形界面，豆包这类关闭了 uiautomator 的 App** |

第三种最重要：豆包会拒绝 `uiautomator dump`（返回 null root node），
读不到任何文字，只能靠模板匹配来验证。

### 实测结果（从豆包完全停止开始）

```
1. 启动 com.larus.nova   4982ms  ✅ 前台=com.larus.nova
2. 点 打电话.png         4311ms  ✅ 出现 开启投屏.png @ (418,2110)
3. 点 开启投屏.png       3233ms  ✅ 出现 开始投屏.png @ (853,1511)
流程结束：全部通过  3/3   共 12527ms
```

反例也测过：动作成功但期望不符时，会明确报 FAIL 并给出实际值。

## 插电自动执行（autowatch）

```sh
./start-autowatch.sh                        # 默认 flows/通话投屏.json
./start-autowatch.sh flows/别的.json         # 换流程
./.venv/bin/python autowatch.py --once      # 立刻跑一次（不管插没插电）
./.venv/bin/python autowatch.py --dry       # 只打印步骤
tail -f logs/autowatch.log                  # 看日志
```

机制：每秒读 `/power` 接口（`dumpsys battery`），从「没插电」变成「插电」时触发一次，
然后 90 秒冷却。比 Tasker 侦测 USB 更稳，因为不依赖 Tasker 的事件配置。

## 为什么没用 Tasker 做触发

Tasker 6.6.3 已装，我给它配好了无障碍 + 悬浮窗权限，但：
- Android 13 上 "Power Connected" 事件需要 Tasker 前台服务常驻，MIUI 会杀
- Tasker 的任务要在它自己的 UI 里手搓，改一次流程就要动手机
- 板子端 autowatch 直接复用 airweb 的模板和验证逻辑，改流程只改 JSON

如果你就是要**完全不接板子**（手机单独用），那才需要 Tasker 方案，见下方"手机单机方案"。

## 手机单机方案（不接板子，可选）

手机上有 Magisk root + Termux(Python 3.14)。做法：
1. Termux 里装 python + opencv-python-headless + numpy
2. 把 `airlib.py` 和模板推到 `/data/data/com.termux/files/home/airtest/`
3. Termux 里跑一个精简版 `flowrun.py`（只保留 tap_template + 模板匹配验证）
4. Tasker「Power Connected」→ 执行 `am start` 或 `termux-*` 命令

代价：Python 3.14 上 opencv 轮子可能要现场编译（20-30 分钟），且 Termux 的
adb 只能连 localhost（要先 `adb tcpip`）。**除非确实要脱离板子，否则不建议。**

## airweb 全部接口

| 方法 | 说明 |
|---|---|
| `GET /` | 网页 |
| `GET /screen.jpg` | 当前画面 |
| `GET /info` `/state` `/power` | 设备信息 / 前台+文字 / 电源 |
| `POST /crop` `/tap` `/tap_verify` `/swipe` `/key` `/text` `/open` | 基本操作 |
| `POST /tap_template` `/find` | 找图并点 / 只找 |
| `POST /verify_after_tap` | 语义核对（app / 文字） |
| `POST /flow/run` `/flow/save` | 跑流程 / 存流程 |
| `GET /flows` `/flow/<name>` | 流程列表 / 读取 |
| `GET /proof/before.jpg` `/proof/after.jpg` | 点击前后对比图 |
| `GET /templates` `POST /upload` `DELETE /templates/<name>` | 模板管理 |

## 最后一步：真的点「开始投屏」（2026-11-12 补）

### 点「开启投屏」之后会发生什么

不是直接开始投屏，而是先弹**系统录屏授权窗**：

```
Sys2017:com.android.systemui/...MediaProjectionPermissionActivity
文字：要开始使用豆包录制或投射内容吗？
取消 / 立即开始
```

必须点 **「立即开始」**，投屏才真的开始。这一步以前的流程没有，所以停在弹窗上。

### 新增动作 tap_text

系统弹窗是标准控件，读得到文字，但**没法做模板图**（每台机器文案/尺寸不同）。所以加了一个动作：

```json
{"do":"tap_text","text":"立即开始","find_timeout":10,
 "expect_not_text":"立即开始","expect_text":"正在共享屏幕"}
```

- `tap_text`：按屏幕文字找中心点再点，`find_timeout` 内轮询
- `expect_not_text`：验证某文字**消失**（弹窗关掉了）
- 两者可同时用：既确认弹窗没了，又确认投屏界面出来了

网页的「加一步」里也加了「点文字（按屏幕文字）」选项。

### 现在的四步流程（flows/通话投屏.json）

| # | 动作 | 验证 |
|---|---|---|
| 1 | 启动 com.larus.nova | 前台 = com.larus.nova |
| 2 | 点 打电话.png | 出现 开启投屏.png |
| 3 | 点 开启投屏.png | 出现文字「立即开始」 |
| 4 | 点文字「立即开始」 | 「立即开始」消失 且 出现「正在共享屏幕」 |

实测（豆包完整冷启动，4/4）：

```
1. 启动 com.larus.nova          4636ms  PASS app
2. 点 打电话.png @(395,2075)     3583ms  PASS template 开启投屏.png @[418,2110]
3. 点 开启投屏.png @(418,2110)   4827ms  PASS text 立即开始
4. 点文字「立即开始」@(859,1515)  8205ms  PASS text 正在共享屏幕 + 立即开始消失
全部通过 4/4   21252ms
```

投屏成功后的屏幕文字：`正在共享屏幕`、`退出屏幕共享`、`屏幕上包括通知在内的所有内容均将被录制`。

### 两个坑（都已修）

**1. 上次没退出就断电，下次会失败**
豆包重启会直接恢复到通话页（`RealtimeCallActivity`），首页的「打电话」根本不存在，第 2 步必然找不到模板。
修法：`open` 动作默认先 `am force-stop` 再启动（`"force_stop": true`，默认开）。

实测最坏情况（豆包卡在通话页）→ 自动复位 → 4/4 通过，22487ms。

**2. 守护进程比网页服务先起来**
日志出现 `读电源状态失败: Connection refused`，autowatch 白跑。
修法：启动时先轮询 `/power` 等 airweb 就绪（最多 120s）；`start-autowatch.sh` 还会自动帮你把 airweb 拉起来。

## 第 5 步：投屏成功后回桌面（2026-11-12 再加）

用户要求：看到「正在共享屏幕」就返回桌面。

豆包投屏用的是 `MediaProjection` 抓**整屏**，回桌面不影响共享 —— 手机上
显示什么，对方就看得到什么。所以投完屏回桌面，手机还能正常用。

### 新增 home 动作

```json
{"do":"home","wait":2,"expect_app":"com.miui.home","expect_casting":true}
```

### 新增 expect_casting：证明投屏真的还在

注意事项：**别用 `dumpsys media_projection`**，这台 MIUI 上投屏进行中它也是
`null`，会误判成没投屏。

可靠的判据是虚拟显示：

```
$ adb shell dumpsys display | grep ScreenCapture
DisplayDeviceInfo{"ScreenCapture": uniqueId="virtual:com.larus.nova,10231,ScreenCapture,0",
  720 x 1280, ... type VIRTUAL, state ON, owner com.larus.nova (uid 10231), FLAG_PRIVATE}
```

只要存在 `owner=<包名>` 且 `state ON` 的 ScreenCapture 虚拟显示，投屏就一定在跑。

### 现在的五步流程（flows/通话投屏.json）

| # | 动作 | 验证 |
|---|---|---|
| 1 | 启动 com.larus.nova | 前台 = com.larus.nova |
| 2 | 点 打电话.png | 出现 开启投屏.png |
| 3 | 点 开启投屏.png | 出现文字「立即开始」 |
| 4 | 点文字「立即开始」 | 「立即开始」消失 且 出现「正在共享屏幕」 |
| 5 | 回桌面（HOME） | 前台 = com.miui.home 且 投屏仍在 |

实测（守护进程自动跑，冷启动）：

```
1. 启动 com.larus.nova      6245ms  PASS app
2. 点 打电话.png             3219ms  PASS template 开启投屏.png
3. 点 开启投屏.png           4820ms  PASS text 立即开始
4. 点文字「立即开始」          7898ms  PASS 正在共享屏幕 + 立即开始消失
5. 回桌面                    3202ms  PASS app=com.miui.home + casting on
全部通过 5/5   25386ms
```

### 又修三个坑

**1. 单次点击会偶发不生效，流程整个失败**
点「开启投屏」有一次点了但弹窗没出来（UI 还没响应），验证 22s 超时后判死。
修法：验证不过就**重做这一步**，默认最多 3 次（`"retry": 3` 可调）。
结果里会记 `retried` 数组，能看出重试过几次。

**2. `/power` 在跑流程时被锁死**
Flask 虽是 `threaded=True`，但所有接口共用全局 `LOCK`。跑流程占住几十秒，
autowatch 每秒问 `/power` 就排队到超时 → 日志刷 `读电源状态失败: timed out`。
修法：新增 `safe_light` 装饰器（独立的 `POWER_LOCK`），`/power` 改用它。
实测流程进行中 `/power` 响应 **75ms / 85ms / 73ms**。

**3. `casting()` 调 `p.shell(..., timeout=15)` 报错**
`airlib.Phone.shell` 不接受 `timeout` 参数，异常被吞掉后显示「读不到 display」，
误报投屏已停。修法：去掉该参数。

## 怎么触发这个功能（三种方式，任选）

### 方式一：按手机音量键两下（推荐，不用电脑）

在手机上调好音量键触发就绪后，**连按两下【音量减】**，板子收到就跑流程。

```bash
./start-keytrigger.sh          # 启动监听（已在后台常驻）
tail -f logs/keytrigger.log    # 看触发日志
```

触发后日志长这样：

```
听到一下 VOLUMEDOWN，再按一下就会触发
>>> 触发：/root/.openclaw/workspace/Airtest/flows/通话投屏.json
开始跑流程「通话投屏」，5 步
  OK 1. 启动 com.larus.nova (6161ms)
  ...
流程结束：全部通过  用时 25293ms  5/5 步通过
```

- 两下间隔要 **1.5 秒内**
- 一次触发后有 **8 秒冷却**，防手抖连按重复跑
- `./start-keytrigger.sh --help` 之类的参数：`./start-keytrigger.sh flows/别的流程.json`
- 想用音量加：`./.venv/bin/python keytrigger.py --key 加`

**为什么能读到音量键**：adbd 的 shell 用户在 `input` 组里，`getevent` 就能读到
`/dev/input/event3`（音量减）和 `event0`（音量加）的 EV_KEY 事件，
不需要装 App、不需要无障碍、不需要 root。

### 方式二：拔插 USB 线（现有的 autowatch）

已经在跑：`./start-autowatch.sh`。拔掉再插上，自动跑一次流程。

### 方式三：网页上点按钮

浏览器打开 `http://192.168.1.83:8123`，流程区点 **▶ 运行并逐步验证**。

### 三种方式对比

| 方式 | 触发动作 | 适合 |
|---|---|---|
| 音量键两下 | 按手机键 | 手边只有手机，最快 |
| 拔插 USB | 拔插线 | 通电即用，全自动 |
| 网页按钮 | 点鼠标 | 在电脑前调试 |

## 拔插 USB 触发：实战踩到的三个坑（2026-11-12 12:29 验证通过）

用户实测拔插，一开始失败。三个真实原因，全部已修：

### 坑 1：控制通道是 USB，一拔线就断

原来 `airlib.DEFAULT_SERIAL = "892e20c9"`（USB 序列号）。拔 USB 瞬间：
```
airtest.core.error.DeviceConnectionError: error: device '892e20c9' not found
```
`/power` 连续 500，autowatch 那两次轮询全废，**正好丢掉「未插电→插电」那一刻**。

修法：`airlib.pick_serial()` 自动优先选**无线**（带冒号的 serial），
USB 只作后备。`AIR_SERIAL` 环境变量仍可强制指定。
同时 airweb 的 `@safe` 捕获到 `not found` / connection 类错误时调 `drop_phone()`，
下次请求重建连接，不会一直拿着死连接。

### 坑 2：冷却期 90 秒太长，把人的操作挡了

12:25:51 跑完一次，12:26:15 用户插上电 → `检测到插电，但还在冷却期，跳过`。
90 秒是给机器防抖的，但人在旁边试的时候根本等不了。

修法：`COOLDOWN` 改 **12 秒**。只挡同一次插拔的重复触发，不挡人。

### 坑 3（最关键）：插 USB 后 MIUI 自己弹全屏对话框

```
com.android.settings/com.android.settings.Settings$UsbDetailsActivity
文字：USB 用于 / 仅限充电 / 传输文件 / 传输照片 / 取消
```
它是**全屏 Activity**，盖住一切。第 4 步找「立即开始」找了 47 秒然后失败：
```
FAIL 4. 屏幕上找不到文字「立即开始」（看到：USB 用于/仅限充电/传输文件/传输照片/取消）
```

修法：新增 `dismiss` 动作，按返回关掉它，放流程第一步：

```json
{"do":"dismiss","dismiss_text":"USB 用于","dismiss_tries":3,
 "wait":1,"expect_not_text":"USB 用于"}
```

### 现在的六步流程

| # | 动作 | 验证 |
|---|---|---|
| 1 | 关掉「USB 用于」弹窗 | 该文字消失 |
| 2 | 启动 com.larus.nova | 前台 = com.larus.nova |
| 3 | 点 打电话.png | 出现 开启投屏.png |
| 4 | 点 开启投屏.png | 出现文字「立即开始」 |
| 5 | 点文字「立即开始」 | 「立即开始」消失 且 出现「正在共享屏幕」 |
| 6 | 回桌面 | 前台 = com.miui.home 且 投屏仍在 |

### 实测（用户手动拔插，守护进程自动响应）

```
12:28:59  已拔出电源
12:29:02  >>> 检测到插电，开始执行流程
12:29:02  开始跑流程「通话投屏」，6 步
12:29:34    OK 1. 已关掉「USB 用于」弹窗 (5900ms)
12:29:34    OK 2. 启动 com.larus.nova (6202ms)
12:29:34    OK 3. 点 打电话.png @ (395,2075) (3254ms)
12:29:34    OK 4. 点 开启投屏.png @ (418,2110) (4857ms)
12:29:34    OK 5. 点文字「立即开始」@ (859,1515) (7960ms)
12:29:34    OK 6. 回桌面 (3198ms)
流程结束：全部通过  用时 31373ms  6/6 步通过
```

注意：**拔插触发时手机几乎一定会弹 USB 对话框**，所以 `dismiss` 必须是第一步，
不只是"锦上添花"。

## 成功率实测（2026-11-12 12:47）

用户问「能保持每次 100% 成功吗」。**不能承诺 100%**，但可以把真实的数字给出来。

### 压测结果：13 次连续，全部通过

| 批次 | 次数 | 条件 | 结果 |
|---|---|---|---|
| 第一批 | 5 | 热状态（刚跑完） | 5/5 通过 |
| 第二批 | 10 | 单双交替：息屏 + 冷启动 | 10/10 通过 |
| 合计 | 15 | | 15/15 |

耗时（10 次样本）：最快 32815ms，中位 34663ms，最慢 34995ms。

每次开跑前都 `am force-stop com.larus.nova`，其中 5 次还额外 `KEYCODE_SLEEP`
息屏 —— 也就是「手机揣兜里、豆包早被系统杀掉、插上线」的真实场景。

### 为了让成功率到这个水平，又补了两步

**1. 息屏时什么都验证不了**

息屏状态下 `mCurrentFocus` 全是 `null`，`foreground()` 读不到任何东西，
所有验证都只能等到超时。实测过：息屏跑 6 步流程 → `FAIL 1/6 85928ms`。

修法：新增 `wake` 动作，放流程第一步：

```json
{"do":"wake","wait":1}
```

内部逻辑：查 `dumpsys deviceidle | grep mScreenOn`；黑屏就发
`KEYCODE_WAKEUP` + `wm dismiss-keyguard`，最多试 3 次。

**2. 插 USB 的弹窗必须在第一步之后立刻清掉**

见上一节。所以顺序是 `wake → dismiss → open → ...`。

### 现在的七步流程

| # | 动作 | 验证 |
|---|---|---|
| 1 | wake 唤醒屏幕 | 屏幕亮 |
| 2 | dismiss 关「USB 用于」弹窗 | 该文字消失 |
| 3 | 启动 com.larus.nova | 前台 = com.larus.nova |
| 4 | 点 打电话.png | 出现 开启投屏.png |
| 5 | 点 开启投屏.png | 出现文字「立即开始」 |
| 6 | 点文字「立即开始」 | 「立即开始」消失 且 出现「正在共享屏幕」 |
| 7 | 回桌面 | 前台 = com.miui.home 且 投屏仍在 |

### 为什么不敢说 100%

这些情况下仍可能失败，都不是代码能完全兜住的：

- **豆包自己弹广告/更新提示**：新弹窗盖住界面，模板找不到
- **系统弹权限框**（首次授权录屏、隐私提示）：文案随版本变
- **网络断了**：豆包进不去通话界面
- **手机重启后首次解锁需要密码**：`wm dismiss-keyguard` 解不开密码锁
- **MIUI 省电策略改了**、**豆包更新改了界面**：模板图全部失效

好消息是**失败是"看得出来"的**：每步都有验证，失败会明确报第几步、
期望什么、实际看到什么，并且日志留档，不会静默点空。

## 中途失败会不会接着做完（2026-11-12 12:58 改，已实测）

用户问「中途失败会继续接着完成吗」。

### 改造前：不会

原来 `stop_on_fail=true`，第一处失败就整轮停下，后面全不跑。只有单步内
重试（`retry=3`），但那是同一个动作重放，救不了"界面被别的页面抢走"。

### 改造后：会，而且会自己找路回来

每步失败后不再直接放弃，而是：

1. **清已知弹窗**：只清白名单 `BLOCKER_TEXTS = ["USB 用于"]`。
   不敢乱按 BACK —— 在通话页按一下就把通话挂了。
2. **找落脚点**：往前找最近一个"有明确特征（`expect_app`/`expect_text`/
   `expect_template`）且刚验证通过"的步骤，通常是 `open`。
3. **重放落脚点**：如果前台已经不是它该有的 App，先按 HOME 收掉临时页，
   再重放 `open` 把 App 拉回前台。
4. **从落脚点下一步继续**：不重点落脚点本身 —— 那些点击多半不幂等，
   再点一次「打电话」可能把通话挂了。
5. 最多 `resume_rounds`（默认 3）轮。

**成败按最终状态判定**：中途失手但补回来了，`all_pass` 就是 true。
用户要的是投上屏，不是零瑕疵。

### 实测：强制制造"设置页抢前台"

流程里插一个 `sleep 12`，在它执行时 `am start -a android.settings.SETTINGS`
把设置页推上前台，然后看它能不能自己爬回来：

```
r1 5. 屏幕上找不到 打电话.png   act=False
>>> 恢复: {"did":["重放第3步把 com.larus.nova 拉回前台"],"failed_at":5,"resume_from":3}
r2 5. 点 打电话.png @ (395,2075)  act=True ver=True
r2 6. 点 开启投屏.png             act=True ver=True
r2 7. 点文字「立即开始」           act=True ver=True
r2 8. 回桌面                     act=True ver=True
all_pass: True | 8/8 | 81237ms | 轮次 2
```

事后确认真的投上屏：`owner com.larus.nova`。

改造后 3 次常规回归：7/7、7/7、7/7，均第一轮完成，无回归。

### 顺带发现一个更严重的坑：WiFi 断了就全瞎

测试中出现 `error: device '192.168.1.119:5555' not found`、`No route to host`。
查下去是**手机 WiFi 被关了**（`Wi-Fi is disabled`）。

因为控制通道已经从 USB 切到无线（拔线不断），
**WiFi 一断，`/power` 就全 500，拔插检测直接失效**。

这次的处置：USB 还连着，用 USB 跑 `svc wifi enable` 救回来，IP 仍是
192.168.1.119。

**这个坑没彻底解决**：真要 WiFi 掉了，只能靠 USB 兜底。
所以 `pick_serial()` 的顺序（无线优先、USB 兜底）不能反过来 ——
反过来就退回"拔线即断"的老毛病。两者只能保一个，选了无线。

## 为什么改看板子的 USB 口（2026-11-12 13:25 实测通过）

用户指出：**板子和手机在同一网段，同时又用 USB 连着**。这是一次关键提醒 ——
两条通道不是摆设，应该互相兜底。

### 原来错在哪

`autowatch` 靠 `adb shell dumpsys battery` 问手机"你插着电吗"。问题：

1. **依赖控制通道先活着**。WiFi 一断，`/power` 全 500，拔插检测直接失效。
   实测踩到：手机 WiFi 被关 → `No route to host` → 完全瞎。
2. **轮询每秒一次**，插拔那一瞬间可能错过。
3. **问的是手机自己的说法**，不是"这条线有没有通"。

### 现在：板子直接看 /sys

手机在板子上的 USB 节点：`/sys/bus/usb/devices/2-1.3`
（`idVendor=18d1` = Google/adb，`serial=892e20c9` = 手机序列号，`product=Xiaomi 12S Ultra`）

拔了目录整个消失，插上就出现。**这是内核级的事实，不经过 adb、不需要网络、不可能骗人。**

`usbwatch.py` 每 0.25s 看一眼这个目录，变化后防抖 2.5s 再动手。

### 实测：真实拔插触发

```
13:25:04 已拔掉 USB
13:25:10 已插上 USB
13:25:10 >>> 检测到插电，开始执行流程（7 步）
13:25:42   OK 1. 屏幕已唤醒        (1123ms, 累计1.1s)
13:25:42   OK 2. 已关掉 USB 弹窗    (6022ms, 累计7.1s)
13:25:42   OK 3. 启动 com.larus.nova (6156ms, 累计13.3s)
13:25:42   OK 4. 点 打电话.png      (3239ms, 累计16.5s)
13:25:42   OK 5. 点 开启投屏.png    (4958ms, 累计21.5s)
13:25:42   OK 6. 点「立即开始」      (7957ms, 累计29.5s)
13:25:42   OK 7. 回桌面            (3232ms, 累计32.7s)
流程结束：全部通过  32691ms  7/7（轮次 1）
```

事后确认 `owner com.larus.nova` —— 真的投上屏了。

### 顺带修掉的三个死锁

**1. 半死连接会让服务永久卡住**（最严重）

WiFi 断了以后，`adb devices` **仍然显示 device**，但任何命令都不返回。
原来的 `Phone.shell()` 直通 airtest，没有超时 → `/power` 永久挂起 → 服务废掉。

修：`shell(cmd, timeout=25)` 改成走 subprocess 自己控超时，超时抛
`设备无响应`，让上层换通道。实测：WiFi 关掉后 `/info` 从"永久卡死"变成
几十秒内报错并自动切到 USB。

**2. `pick_serial()` 信任了 `adb devices` 的假象**

现在先 `alive(serial)` 真发一条 `echo ok` 探活（6s 超时），探不通就换下一个候选。
实测：WiFi 断时 `192.168.1.119:5555 alive=False`、`892e20c9 alive=True`，
自动选中 USB。

**3. 无线掉了不会自愈**

新增 `ensure_wifi()`：无线不在、USB 在，就用 USB 发 `svc wifi enable`，
等 IP 出来再 `adb connect` 回去。`heal_phone()` 在连接报错时调用它。

**注意 `DEFAULT_SERIAL` 还留在 airlib 里但已不用** —— `Phone.__init__`
现在每次 `pick_serial()`，避免 import 时就把通道焊死（原来 WiFi 中途换了它不认）。

### 现在的分工

| 脚本 | 触发方式 | 说明 |
|---|---|---|
| `usbwatch.py` | 板子 USB 口插上 | **推荐**，不依赖 adb/网络 |
| `keytrigger.py` | 手机音量减按两下 | 不方便拔线时用 |
| `autowatch.py` | adb 读电池 | **已停用**，与 usbwatch 重复 |
| 网页按钮 | 点一下 | 手动 |

`autowatch` 已 pkill，避免和 `usbwatch` 对同一次插拔各跑一遍。

## 真实场景：板子通电 → 手机自动开机 → 自动投屏（2026-11-12 13:34 全链路实测）

用户说明的真实用法：

> 手机每次都会关机，rk3588s 每次都不关机。要学习的时候我就把 rk3588s 通电，
> USB 连着手机在会自动开机，然后打开豆包流程。

这比我之前测的"拔插"难得多。**手机是关机的**，所以：

1. 板子必须在没人登录的情况下自己把服务拉起来 → **systemd**
2. USB 节点一出现 ≠ 手机能用，Android 还要几十秒才起来 → **必须等 `sys.boot_completed=1`**
3. 板子开机时手机已经插着，永远等不到"拔→插"的跳变 → **启动时若已插着就先跑一次**

### 三个改动

**1. systemd 开机自启**

`/etc/systemd/system/airtest-autocast.service`（`Type=oneshot` +
`RemainAfterExit=yes`，`WantedBy=multi-user.target`，已 `enable`）

```ini
After=network-online.target
Wants=network-online.target
WorkingDirectory=/root/.openclaw/workspace/Airtest
ExecStart=/root/.openclaw/workspace/Airtest/start-all.sh
ExecStop=/root/.openclaw/workspace/Airtest/stop-all.sh
Environment=ANDROID_HOME=/usr/lib/android-sdk
```

`start-all.sh` 依次：拉 adb server → 从 USB 侧问手机 IP 并 `adb connect`
→ `start-web.sh` → `start-usbwatch.sh` → `start-keytrigger.sh`。
`stop-all.sh` 负责干净地停掉三个。

**2. 等手机真开机（`wait_android_boot`）**

只看 `/sys` 的 USB 节点不够：手机一上电，USB 立刻枚举，但 Android 还没起来。
现在 `run_flow()` 先等 `getprop sys.boot_completed == 1`，最多 240 秒，
每 15 秒打一次"等手机开机… 已等 Ns"。

**3. 启动时已插着就立刻跑**

原来只在"拔→插"跳变时触发。用户场景里手机一直插着，跳变永远不发生。
现在 `main()` 里判断 `present` 就直接跑一次。

### 全链路实测：真关手机

```
13:32:5x  adb -s 892e20c9 shell "reboot -p"      # 手机彻底关机
          2 秒后：adb=0  usb节点=0               # 真的断电了
13:34:13  等 airweb 就绪…
13:34:16  当前状态：已插着
13:34:16  启动时已插着，先跑一次                  # ← 手机在自动开机
13:34:22  手机已开机完成（等了 6s）               # ← 等到 boot_completed=1
13:34:22  >>> 开始执行流程（7 步）
13:34:56  OK 1 屏幕已唤醒      (1231ms, 累计1.2s)
13:34:56  OK 2 关 USB 弹窗     (6096ms, 累计7.3s)
13:34:56  OK 3 启动豆包        (6214ms, 累计13.5s)
13:34:56  OK 4 点打电话        (4317ms, 累计17.9s)
13:34:56  OK 5 点开启投屏      (4904ms, 累计22.8s)
13:34:56  OK 6 点立即开始      (7905ms, 累计30.7s)
13:34:56  OK 7 回桌面          (3288ms, 累计34.0s)
流程结束：全部通过  33959ms  7/7（轮次 1）
```

事后确认：`owner com.larus.nova`（真的投屏中）、前台 `com.miui.home`、
WiFi 自动连回 `192.168.1.119`。

### 还没验证的一环：板子自己断电重启

我做不到 —— **给板子断电会同时关掉我所在的 Gateway**，重启过程我无法观察。
这一环要用户自己试。板子重启后：

```bash
systemctl status airtest-autocast        # 应该是 active
tail -20 logs/usbwatch.log               # 应该看到"启动时已插着，先跑一次"
```

如果没起来，先看 `logs/service.log`：最常见是 adb server 抢在网卡前面，
或 venv 路径不对（`WorkingDirectory` 必须对）。

### 踩到的坑：`systemctl start` 对 active 的 oneshot 是空操作

我停掉服务后 `systemctl start`，结果什么都没起 —— 因为单元状态还是
`active (exited)`（`RemainAfterExit=yes` 的语义），`start` 直接返回成功不动手。
要重新拉起来必须 `systemctl restart`。

## 冷启动实测通过（2026-11-12 13:39 用户真实断电）

用户拔掉手机 USB、给 rk3588s 直接断电，然后重新通电。

```
板子开机      13:39:45   （uptime -s）
启动时已插着，先跑一次
手机已开机完成（等了 6s）
流程结束：全部通过  7/7 步通过
事后：owner com.larus.nova  ✓   前台 com.miui.home ✓
```

开机自启、等待手机开机、执行流程、投屏，整条链路无人干预跑通。

### 但冷启动暴露一个真 bug：没网时 RTC 是错的，NTP 一跳时钟就跳

板子没有电池 RTC，冷启动时系统时间从 13:39 左右起来，**比真实时间慢 22 分钟**，
日志写的是 `13:17:24`。等网络通、NTP 校时后时间**瞬间前跳到 13:40:51**。

因为所有超时都用 `time.time()` 做 deadline，这一跳会导致：

- `deadline = time.time() + 12` 在前跳前算好 → 前跳后立刻"超时"
- 更糟的是反向跳变会让 `time.time() - t0` 变成 **1368012ms（22 分钟）**

实测就出现了这样一行荒谬日志：

```
OK  1. 屏幕已唤醒 (1368012ms, 累计1368.0s)
流程结束：全部通过  用时 1400818ms    ← 实际只有 34 秒
```

功能上这次侥幸没错（第 1 步在跳变前就做完了），但**如果跳变发生在超时判断中间，
就会误判为超时，把成功的步骤当成失败**。

**修复**：所有计时/超时改用 `time.monotonic()`（单调时钟，不受校时影响），
日志时间戳仍用 `time.strftime`。共改 `airlib.py` 2 处、`airweb.py` 15 处。
修复后重跑：

```
流程结束：全部通过  用时 34118ms  7/7 步通过
```

**教训**：`time.monotonic()` 才是计时用的，`time.time()` 只能当"现在几点"。
在没有 RTC 电池的板子上这不是洁癖 —— 每次冷启动都是一次校时跳变。

## 开机校时（2026-11-12 13:45 实测）

### 根因：这块板子的 RTC 电池是失效的

```
dmesg:
[6.559726] rtc-hym8563 6-0051: rtc information is invalid
[6.567393] rtc-hym8563 6-0051: setting system clock to 2021-01-01T12:00:00 UTC
```

芯片是 `rtc-hym8563`（I2C 6-0051），但**每次冷启动内核都判定数据无效，
直接把系统时间设成 2021-01-01 12:00:00**。所以之前那次冷启动日志里的
`13:17` 其实是"2021 年的 13:17"，随后 NTP 校时把它拉到 2026-11-12 13:40，
造成 22 分钟的跳变（更准确说是跨了 5 年）。

系统本来是配了 NTP 的（`ntp.aliyun.com` / `ntp.tencent.com` / `cn.pool.ntp.org`，
`systemd-timesyncd` enabled + active），**但 NTP 要等网络通、还要几十秒才收敛**，
这期间写下的日志时间全是 2021。

### 解法：开机时抢先校时

新增 `airtest-timesync.service`（`Before=airtest-autocast.service`，
`WantedBy=multi-user.target`，已 enable），执行 `timesync.sh`：

1. 判断时间是否可信：年份 ≥ 2024 且 `NTPSynchronized=yes`
2. 不可信就先等网络（最多 30s）
3. **先用 HTTP `Date` 头抢时间**（`https://www.aliyun.com` → 腾讯 →
   最后兜底 `http://192.168.1.1` 路由器）。比 NTP 快，且走已通的那条网
4. 再 `systemctl restart systemd-timesyncd` 精调
5. 脚本容错：HTTP 成了但 NTP 没确认也算成功

`airtest-autocast.service` 已改成 `After=` + `Wants=airtest-timesync.service`，
保证顺序。

### 实测

**NTP 正常时**（把时间设成 2021-01-01 再跑）：

```
[2021-01-01 12:00:00] 时间不可信（...），开始校时
[2026-11-12 13:44:39] 已按 http://192.168.1.1 的 Date 头设时间
[2026-11-12 13:44:41] NTP 已同步
[2026-11-12 13:44:41] 校时完成（NTP 同步）        ← 2 秒
```

**外网断掉时**（iptables 封 123/udp + 一个外网 IP）：仍靠网关 `192.168.1.1`
的 Date 头校回正确时间。

**完整开机顺序模拟**（时间设成 2021 → `start timesync` → `start autocast`）：

```
模拟开机现场: 2021-01-01 12:00:00
timesync 完成: 2026-11-12 13:45:03
autocast 启动
流程结束：全部通过  33696ms  7/7 步通过
```

`grep -c "2021-" logs/usbwatch.log` → **0**。流程日志全部是正确时间，
耗时 `33696ms`（不再是 1400818ms 那种被跳变污染的数）。

### 补充：RTC 长期怎么办

`timesync.sh` 里尝试写回 RTC 的那步可以真正确保"装上电池后立刻生效"。
如果不想每次开机都靠网络，给板子装一颗 CR2032（`hym8563` 支持），
RTC 就能保持时间，`rtc information is invalid` 也会消失。
