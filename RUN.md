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

## 已发布到 GitHub（2026-11-12 13:50）

https://github.com/chengmeiqq/rk3588-autocast

### 为什么是独立仓库，而不是这个目录

`/root/.openclaw/workspace/Airtest` 是 **Airtest 官方仓库的克隆**
（remote 指向 `AirtestProject/Airtest`，506 个上游文件）。我们的代码是叠加
在它上面的自动化项目，**绝不能推到上游**。

所以抽成独立仓库：27 个自有文件（脚本、流程、模板、文档），
依赖通过 `requirements.txt` 装官方 `airtest` 包。

| 提交到 GitHub | 留在本地 |
|---|---|
| `airweb.py` `airlib.py` `usbwatch.py` `keytrigger.py` `autowatch.py` `airctl` | `.venv/`（245M） |
| `flows/` `templates/` `macros/` | `logs/`（8.5M） |
| `start-*.sh` `stop-all.sh` `timesync.sh` `env.sh` | `*.bak` |
| `README.md` `RUN.md` `requirements.txt` `ui.html` | 上游 506 个文件 |

验证：`git rev-parse HEAD` == `git ls-remote origin refs/heads/master`
（`7222144`）；`logs`/`.venv`/`*.bak`/大图 在远端全部 404。

### 两处为公开做的调整

- `airlib.py` 的设备序列号兜底从硬编码改成读 `PHONE_SERIAL` 环境变量
  （`usbwatch.py` 本来就是这么做的）
- `macros/demo-home.png`（3.4M 演示原图，含手机界面）没上传，
  加进了 `.gitignore`

### 安全提醒

提供的 token 权限范围很大，包含 `delete_repo`、`admin:org`、
`admin:enterprise`、`workflow`。只用到 `repo` 就够。建议换成
细粒度 token（只勾 `rk3588-autocast` 的 Contents: read/write），
并轮换这个。用完已 `shred` 销毁临时文件，未写入 `.git/config`
或 remote URL；本地记忆文件里的明文也已抹除。

## 14:22 那次失败的真实原因（2026-11-12 排查）

现象：只打开了豆包，第 4 步「打电话」没做，日志报 3 次
`BrokenPipeError: [Errno 32] Broken pipe`，3/7 步通过后退出。

### 时间线（dmesg + 手机 btime 对齐）

```
14:22:23  USB disconnect, device 9
14:22:39  重新枚举 idVendor=18d1 (Android)
14:22:43  usbwatch 开跑（手机 btime=14:22:25，只开机 18 秒）
          1 唤醒 ✓   2 关 USB 弹窗 ✓   3 启动豆包 ✓
          4 点「打电话」✗ BrokenPipeError (709ms / 280ms / 342ms)
14:23:05  3/7 失败退出
14:26:43  USB disconnect, device 10
14:27:24  重新枚举 idVendor=05c6 idProduct=f000 ← 关机充电模式
```

`05c6:f000` + `bInterfaceClass=08 (MassStorage)` 是手机**完全关机**后
只剩充电芯片在跑的枚举结果，adb 根本连不上。

### 三个层次的原因

**1. `sys.boot_completed=1` 不等于系统能用（真 bug）**

手机 btime 14:22:25，流程 14:22:43 开跑 —— 只启动 18 秒。
`boot_completed` 已经是 1，但 14:02 那次留下了铁证：

```
cmd: Can't find service: window
```

`window` 服务还没注册，`screencap` 必然失败，这就是 `BrokenPipeError`
的来源。我原来只等 `boot_completed`，判据太弱。

**2. 供电严重不足 —— 手机是被饿死的（根因）**

```
USB powered: true
Max charging current: 500000   ← 500mA
Max charging voltage: 5000000  ← 5V
```

板子 USB 口只给 **5V/0.5A = 2.5W**。12S Ultra 亮屏 + 跑 App + 投屏编码，
功耗远超 2.5W。所以手机是**边充电边放电**，电量慢慢掉到 25%，
电压撑不住就关机（14:26:43 掉线，14:27:24 以 05c6 回来）。

这也解释了为什么之前几次成功、这次失败：**电量越低越容易触发**。

**3. Airtest 截图没有超时（放大器）**

`self._dev.snapshot()` → `Popen(...).communicate()`，**没有 timeout**。
USB 一断管道就裂，抛 `BrokenPipeError`，而且死连接会被反复复用，
3 次重试全撞在同一个坑上。

### 修复

**A. 真·就绪判据**（`airlib.system_ready`）

不只是 `boot_completed`，还要：

- `service check window` / `package` / `activity` 都在
- `dumpsys window | grep mCurrentFocus` 有焦点窗口（不是 null）

加 `wait_system_ready(settle=15)`：服务就绪后再稳 15 秒，
避开首屏动画和资源加载。usbwatch 在跑流程前调用。

**B. 截图自己控制超时**（`Phone._raw_screencap`）

不再走 `self._dev.snapshot()`，改成 `subprocess.run(..., timeout=25)`
直接 `adb exec-out screencap -p`，自己解码。断了就
`_wait_device_back()`（最多 20s）+ `reconnect()` 再试一次。

实测：设备消失时 **25.9 秒有界报错**，不再卡死。

**C. 稳定期 15 秒**

服务在了 ≠ UI 能点。

### 修复后实测

```
14:32:22  启动时已插着，先跑一次
14:32:37  系统就绪（等了 0s，另稳定 15s）
14:33:14  流程结束：全部通过  36768ms  7/7 步通过
          投屏 owner com.larus.nova ✓
```

比原来的 33.7 秒多了约 3 秒（稳定期），换来的是不会再撞未就绪的系统。

### 但供电这一条代码修不了

手机现在仍是 25% 电量、`Max charging current: 500mA`。
**正确做法是给手机单独接充电器**，别指望板子的 USB 口。
否则电量低的时候一定还会复现"跑到一半关机"。

保底措施：跑流程前检查电量，低于阈值就先提示（见下）。

## 保底：跑流程前查电量

`usbwatch.py` 在跑流程前读一次电量，低于 `MIN_BATTERY`（默认 20）
就跳过并记日志，避免把手机跑没电反而更麻烦。

## 后续复核：供电没问题，投屏是被"上滑清理"杀的

前一节我把 14:23 失败归因到"500mA 供电不足导致关机"。**这个结论是错的**，
实测数据不支持。

### 供电其实够

连着 USB、投屏正在跑的情况下采样 5 次：

```
14:43:54  Charge counter 921mAh  level 25%  cast=owner com.larus.nova
14:45:13  Charge counter 924mAh  level 26%  cast=owner com.larus.nova
14:46:06  Charge counter 926mAh  level 26%  cast=owner com.larus.nova
14:46:53  Charge counter 928mAh  level 26%  cast=owner com.larus.nova
14:47:53  Charge counter 931mAh  level 26%  cast=none
```

**电量一直在涨**（约 1.4mAh/分钟）。500mA 是够用的。

另一条反证：这次开机 `btime=1794493782`（14:29:42），连续运行 17 分钟没重启，
`getprop ro.boot.bootreason` = `shutdown,userrequested`（人为关机）。

### 投屏会被任务管理器清掉（真问题）

投屏存活测试：7/7 通过后每 30 秒采样一次。

```
14:52:33  cast=owner com.larus.nova  pid=10953
14:53:03  cast=owner com.larus.nova  pid=10953
14:53:33  cast=owner com.larus.nova  pid=10953
14:54:03  cast=owner com.larus.nova  pid=10953
14:54:33  cast=owner com.larus.nova  pid=10953
14:55:03  cast=owner com.larus.nova  pid=10953
14:55:33  cast=owner com.larus.nova  pid=10953
14:56:04  cast=NONE                   pid=DEAD     ← 约 3.5 分钟后没了
```

`logcat -b events` 里的真实原因：

```
22:55:40.984 ProcessSceneCleaner: SwipeUpClean: kill procName=com.android.settings
22:55:43.943 ProcessSceneCleaner: SwipeUpClean: force-stop pkgName=com.larus.nova
22:55:43.955 am_kill: [0,10953,com.larus.nova,200,stop due to SwipeUpClean]
```

**`SwipeUpClean` = 从最近任务上滑清理**。前面还有 `com.android.settings.
Settings$AppNotificationSettingsActivity` 的痕迹，说明有人在手机上操作过。

另有一种更隐蔽的：MIUI `SmartPower` 也会把后台的豆包判成 `hibernation`：

```
22:47:50 SmartPower: com.larus.nova/10231 state=hibernation adj=100
22:47:53 SmartPower.com.larus.nova/10231(10949): background->idle(3008ms)
```

### 已做的缓解

```
adb shell dumpsys deviceidle whitelist +com.larus.nova   # 加 doze 白名单
adb shell settings put global app_standby_enabled 0       # 关应用待机
```

doze 白名单不拦 MIUI 自己的 `ProcessSceneCleaner`。真要彻底防，得在
手机侧给豆包设「无限制」+ 锁定后台，或者用 `am set-inactive` 反向标记。

### 结论

- **不是供电问题**，别为这个去改硬件（用户已决定改直供电，那是另一回事，更稳）。
- **流程本身 7/7 稳定通过**，多次实测 32.8s / 32.9s / 33.1s / 35.7s。
- 投屏能撑住的前提是**没人手动清理后台**。自动化本身不会去清。

## 手机侧手动设置：防止豆包被系统清掉

投屏能撑多久，取决于豆包进程活多久。板子侧无法完全管住这个，
必须在**手机上手点一次**下面这些。小米 12S Ultra / MIUI（HyperOS）路径如下。

包名：`com.larus.nova`（豆包 15.7.0）

### 1. 省电策略设为「无限制」（最重要）

设置 → 应用设置 → 应用管理 → 豆包 → **省电策略 → 无限制**

这是治 `SmartPower` 把进程判成 `hibernation` 的那个问题。
默认「智能限制」会在后台几秒后就把进程压到 idle。

### 2. 锁定后台（防止上滑清理）

打开豆包 → 进入最近任务（多任务界面）→ 长按豆包卡片 → 点**锁图标** 🔒

治的是 `ProcessSceneCleaner: SwipeUpClean`。锁住之后上滑「清理全部」
不会杀它。这一步只有手机能干，adb 改不了。

### 3. 自启动 + 后台弹出界面

设置 → 应用设置 → 应用管理 → 豆包 → **自启动**：打开
同页面 → **其他权限** → 「后台弹出界面」「显示悬浮窗」：打开

### 4. 电池优化白名单（已用 adb 代做）

```
adb shell dumpsys deviceidle whitelist +com.larus.nova
adb shell settings put global app_standby_enabled 0
adb shell cmd appops set com.larus.nova RUN_IN_BACKGROUND allow
adb shell cmd appops set com.larus.nova RUN_ANY_IN_BACKGROUND allow
adb shell am set-inactive com.larus.nova false
```

已执行并验证：

```
dumpsys deviceidle whitelist  →  user,com.larus.nova,10231
RUN_IN_BACKGROUND             →  allow
RUN_ANY_IN_BACKGROUND         →  allow
am get-inactive               →  Idle=false
```

但**这几条管不住 MIUI 自己的 `ProcessSceneCleaner`**，所以第 1、2 步
必须在手机上手动做。

### 5. 关闭「锁屏后清理内存」

设置 → 省电与电池 → 齿轮（右上角）→ **锁屏后清理内存 → 从不**

### 验证方法

设置完跑一次流程，然后每 30 秒采样：

```bash
adb -s 892e20c9 shell "dumpsys display | grep ScreenCapture" | grep -o "owner [a-z.]*"
adb -s 892e20c9 shell "pidof com.larus.nova"
```

`owner com.larus.nova` 和 pid 同时存在超过 10 分钟，说明设置生效了。
如果又出现 `SwipeUpClean`，用这条确认是哪种清理：

```bash
adb -s 892e20c9 shell "logcat -d -b events | grep -iE 'am_kill' | grep larus | tail -5"
```

### 补充：还有一种清理叫 OptimizationClean

除了 `SwipeUpClean`（手动上滑），MIUI 还会主动做一次「优化清理」：

```
23:01:12.692 ProcessSceneCleaner: OptimizationClean: kill procName=com.xiaomi.security.onetrack
23:01:12.694 ProcessSceneCleaner: OptimizationClean: kill procName=com.android.smspush
23:01:12.694 ProcessSceneCleaner: OptimizationClean: kill procName=com.google.android.ext.services
23:01:12.695 ProcessSceneCleaner: OptimizationClean: kill procName=com.android.permissioncontroller
23:01:12.695 ProcessSceneCleaner: OptimizationClean: force-stop pkgName=com.larus.nova
23:01:12.697 am_kill: [0,17227,com.larus.nova,200,stop due to OptimizationClean]
```

注意它**连系统组件一起杀**（`permissioncontroller`、`smspush`），所以这不是
针对豆包的，是 MIUI 的一次批量清理。

而且当时内存**一点都不紧张**：

```
MemTotal:       11444316 kB   (约 11GB)
MemAvailable:    7055264 kB   (约 7GB 可用)
```

7GB 可用还触发清理，说明这是**定时/场景触发**的策略，不是内存不足导致。

### 两种清理的区别与应对

| 触发源 | 含义 | 怎么防 |
|---|---|---|
| `SwipeUpClean` | 用户在最近任务上滑清理 | 锁定后台任务（手机上点 🔒） |
| `OptimizationClean` | MIUI 主动批量优化 | 省电策略→无限制 + 关「锁屏后清理内存」 |

两者都可能导致投屏中断。第一种靠锁任务卡，第二种只能靠省电策略。

### 重要：用户操作手机时不要跑自动化

本次观测到，投屏被杀的那几次，手机前台都在变：

```
22:55:33  前台 = com.android.settings.Settings$AppNotificationSettingsActivity
22:55:39  前台 = com.miui.home/.launcher.Launcher  (RecentsAnimation.onAnimationFinished)
23:01:29  前台 = LauncherOverlayWindow:com.miui.personalassistant   (负一屏)
23:01:29  前台 = com.android.quicksearchbox.SearchActivity
```

**结论：手机在被人工操作时，投屏必掉。** 自动化适合无人值守场景
（手机放着、没人碰），不适合边用边跑。

---

## 大模型 AI 与模板融合（2026-11-12 晚）

### 为什么要融合，而不是纯 AI

纯大模型做点击实测不可靠，两个硬数据：

- **坐标会飘。** 同一张图问「打电话」在哪，裸图答 `(365,868)`，
  真值 `bounds=[364,2067][481,2103]` 中心 `(422,2085)`，差 1200 多像素。
  那次点下去竟然是成功的，纯属侥幸。
- **认不出图标。** 通话页底部那颗「开启投屏」（方框带向上箭头），
  模型三次都答"没有投屏图标"，甚至说成"上传已禁用"。

给图片叠加 100px 坐标网格（红竖线标 x、绿横线标 y，线上写真实像素）后，
同一个问题答 `(370,2100)`，落在按钮 bounds 内——**能用了**。

但模板匹配的精度本来就是 0 误差（`打电话.png` 0.999、`开启投屏.png` 0.998），
所以分工是：

| 事情 | 谁干 | 为什么 |
|---|---|---|
| 找按钮坐标 | 模板优先，模板找不到才问模型 | 模板误差 0；模型要网格才准 |
| 核对模型给的坐标 | 模板 | 偏差超 130px 就认为点错，不点 |
| 判断当前是什么页面 | 模型 | 「首页 / 会话详情页 / 通话页」只有它看得懂 |
| 点错之后怎么救 | 模型 + 重启豆包 | 实测按 BACK 会退到桌面，越走越远 |
| 确认真的开好了 | `dumpsys` + 模型看图 | 两边都说在共享才算赢 |

### 踩过的坑（都是实测）

1. **AI 验证放在「回桌面」之后 → 误判**
   流程 8/8 全过、投屏真的开着，但最后一步回桌面后，「正在共享屏幕」
   横幅消失了，只剩状态栏一个几像素的小图标，模型答"没看到"，
   于是一次成功的投屏被判失败、白重跑两轮（每轮 35 秒）。
   **修法**：给 `airweb` 加 `ai_cast` 动作，插在「回桌面」之前（流程变 9 步）。
   桌面上的复查再加一层"放大状态栏 3 倍"兜底。

2. **重复点「开启投屏」会把投屏关掉**
   点完图标后权限框有时要几秒才弹，旧代码 2.5 秒没等到就再点一次，
   但那个位置此刻已经变成「停止投屏」了。三次重复点击的结果是
   "点完了但系统里没有投屏会话"。
   **修法**：点过图标就绝不再点，只轮询等结果（最多 16 秒）；
   要重试就整轮推倒——重启豆包，界面干净再来。
   改完单轮耗时从 40–138 秒降到 16–22 秒。

3. **冷启动会停在侧栏抽屉，按 BACK 直接退到桌面**
   `monkey` 拉起豆包有时停在左侧抽屉，按一次 BACK 不是回首页而是回桌面，
   下一轮模型看到桌面又想点图标，越走越远。
   **修法**：`_restart_home()`——`am force-stop` 后 `am start -n
   com.larus.nova/com.larus.home.impl.alias.AliasActivity1`，比 monkey 稳。

### 测试结果

```
smartcast 单独连跑 5 次：      5/5 通过，每轮 16–22 秒
完整链路连跑 5 次：            5/5 通过，全部第 1 次尝试即成功
                              （模板流程 37.6 秒 + AI 确认 4 秒）
```

### Tasker 怎么用

板子上 `smartcast.py` 常驻在 `8130`，Tasker 只需要一个 **HTTP Request**：

```
POST http://192.168.1.83:8130/cast
```

返回 `{"ok": true, ...}` 即投屏已确认开好。探活用 `GET /health`。

Tasker 负责前置（唤醒、开豆包、关 USB 弹窗），`smartcast` 只做
「点打电话 → 点开启投屏 → 确认投屏」三件事——但即使 Tasker 前置没做好，
它也会自己拉起豆包兜底。

### 双保险

`usbwatch.run_flow()` 里，模板流程失败时会自动切到 `smartcast`：

```
>>> 模板流程没成，切 AI 融合路径
    [cast] 模板没命中，重启豆包回首页
    [cast] 第2轮：模板命中「打电话」@(395,2075) 分数 0.999
```

实测有一轮模板流程只过 5/9（点「打电话」后没进通话页），
融合路径接住并最终开出投屏。

---

## 2026-11-13 重构：从「一次 oneshot」改为「三个常驻服务 + 投屏看门狗」

### 为什么改

11-13 早上板子通电后完全没有反应。查日志发现两件事：

**1. `usbwatch` 启动后自杀。** 它用 `airweb` 的 `/power` 接口探测服务是否就绪，
但 `/power` 内部要连手机，手机不在 USB 上时返回 500。于是它等 120 秒后判定
「airweb 没起来」，自己退出 —— 而它正是那个「等手机插上就开始干活」的进程。

```
2026-11-13 07:50:33 airweb 一直没起来，先退出
```

**2. 投屏成功后会断，断了没人重拉。** logcat 记录：

```
11-13 06:50:04 建立 → 06:55:04 销毁   5分00秒
11-13 15:58:22 建立 → 16:03:22 销毁   5分00秒
```

`MediaProjectionManagerServiceStubImpl` + `Cancel FGS notification
ChannelId:ScreenCaptureChannel` → 是 App 主动收尾（`onRelease`），非崩溃。
但 08:20 那次超过 6 分钟没断，说明**不是豆包硬性 5 分钟上限**，
更像通话被挂断或手机侧清理。无论如何，断了必须能自动重来。

### 三处改动

**1. usbwatch 探测改 `/`，且永不自杀**

```python
# 旧：探测 /power，超时就 return 1 退出
# 新：探测 /（永远 200，不碰设备），且无限等下去
while True:
    rc = curl ... url + "/"
    if rc == "200": break
    waited += 1; time.sleep(1)
```

理由：`oneshot + RemainAfterExit` 下进程死了 systemd 不管，绝不能自己退出。

**2. 加投屏看门狗（`CAST_WATCH=1`）**

新函数 `cast_owner_now()` 只跑一次 `dumpsys display | grep ScreenCapture`
取 owner，几百毫秒，不截图不问模型（守护循环不能烧钱）。

主循环每 `CAST_CHECK_EVERY`（默认 20 秒）查一次；之前在投屏、现在 owner 空 →
`rearm("掉线重拉")` 重跑整轮。抽了内部函数 `rearm(reason)` 统一更新
`last_run` / `cast_up` / `next_cast_check`，避免「流程说成功但 owner 已空」的假状态。

实测（手动 `am force-stop com.larus.nova` 模拟被清理）：

```
08:26:24 投屏不在了（owner 空），重新拉起
08:26:26 >>> 掉线重拉
08:27:21 流程结束：全部通过  9/9 步通过（轮次 1）
```

2 秒发现，55 秒恢复。

**3. 一个 oneshot 拆成三个常驻服务**

原来 airweb / usbwatch / smartcast 全塞在 `airtest-autocast.service` 里
（`Type=oneshot` + `RemainAfterExit=yes`）—— 进程死了 systemd 不管，
这正是「服务显示 active 但实际已死」的根因。

现在（全部 `Restart=always`、`RestartSec=5`、`enabled`、`multi-user.target`）：

| 单元 | ExecStart | 依赖 |
|---|---|---|
| `airtest-airweb.service` | `.venv/bin/python ./airweb.py 8123` | `After=network-online.target airtest-timesync.service` |
| `airtest-smartcast.service` | `./smartcast.py --serve 8130` | `Requires=airtest-airweb.service` |
| `airtest-usbwatch.service` | `./usbwatch.py`（`CAST_WATCH=1`） | `After=… airtest-airweb.service` |

`start-all.sh` 里启动 usbwatch 那一步已注释掉，改由独立服务拉起；
旧的 `airtest-autocast.service` 已 `disable`（否则开机会抢 8123 端口）。

崩溃自愈实测：`kill -9` 掉 usbwatch → systemd 5 秒内拉起新 pid，
`systemctl show -p NRestarts` = 2。

### 两个 unit 文件配置坑（已修）

1. **`StartLimitIntervalSec=0` 必须放 `[Unit]` 段**，放 `[Service]` 会被
   systemd 245 静默忽略（`systemd-analyze verify` 报 `Unknown key name`）。
   后果：重启次数上限实际生效，连续崩溃后 systemd 会彻底放弃拉起。
2. `airtest-usbwatch.service` 的 `After=` 里残留了已 disable 的
   `airtest-autocast.service`，已删。

现在 `systemd-analyze verify` 对三个 airtest 单元无告警。

### 小米 ROM 上判断投屏的正确方式

**`dumpsys media_projection` 返回 `null`，不能用它判断。** 要用：

```bash
adb shell "dumpsys display | grep ScreenCapture"
# → DisplayDeviceInfo{"ScreenCapture": ..., state ON, owner com.larus.nova (uid 10231), ...}
```

`smartcast.casting_owner()` 用的就是这个，正确。

### 冷启动实测（2026-11-13 08:34）

`systemd-run --on-active=5 systemctl reboot` 让板子真重启（脱离 exec 会话，
避免重启命令被会话 SIGTERM 带走）。开机于 `08:34:53`，结果：

```
服务：airtest-airweb/smartcast/usbwatch/timesync  全部 active+enabled
端口：8123、8130 正常监听
HTTP：airweb / = 200；smartcast /health = {"ok": true, ...}
日志：08:35:03 盯 USB 口 → 08:35:04 airweb 就绪 → 08:35:04 当前状态：没插
```

**服务层完全自动，无需人工登录。** 但手机 USB 又一次枚举失败：

```
[    8.559964] usb 2-1.3: device descriptor read/64, error -32
[    8.899375] usb 2-1-port3: attempt power cycle
[   11.352737] usb 2-1-port3: unable to enumerate USB device
```

`error -32` = EPIPE。软手段（`echo 0/1 > authorized` 重授权多轮）全部无效，
只有**物理拔插**能恢复。这是硬件/供电层面问题，软件救不了。

手机本身在 WiFi 上活着（`192.168.1.119` ping 通、ARP REACHABLE），
但无线 adb 是关的（`persist.adb.tcp.port` 已清空，5555 `Connection refused`）。

**结论**：若断电重启常犯 `error -32`，需硬件侧方案（带独立开关的 USB HUB，
开机脚本先断电再上电），纯软件无解。

---

## 2026-11-13 12:50~13:00 固化与全链路重启验证

### 目标
把「USB 用途框反复弹出」「Tasker 被 MIUI 杀掉」两个问题彻底关掉，
并把所有服务固化到开机自启，用**真实重启板子**验证。

### 已落地的改动

| 文件 | 改动 |
|---|---|
| `keepcast.py` | 新增 `usb_dialog_watch()`、`tasker_watch()`、`ensure_usb_receiver_disabled()`、`check_receiver_after_boot()` |
| `smartcast.py` | `usb` 步骤 + `_run_locked` 持锁包装 |
| `deploy/airtest-keepcast.service` | 新增 |
| `deploy/airtest-adbd.service` | 新增 |
| `deploy/airtest-postboot-check.service` | 新增（oneshot 自检） |
| `scripts/postboot_check.sh` | 新增 |

### 板子重启验证（真实执行，非模拟）

重启前必须先确认**网关能自己回来**，否则会彻底失联：

```bash
export XDG_RUNTIME_DIR=/run/user/0
systemctl --user is-enabled openclaw-gateway.service   # 期望 enabled
loginctl show-user root | grep Linger                  # 期望 Linger=yes
ls /var/lib/systemd/linger/root                        # 必须存在
```

三项缺一不可 —— 这是**用户级**服务，没有 linger 时开机不会启动。

重启命令（`nohup ... &` 不行，会被 exec 会话一起 kill，板子根本不重启）：

```bash
systemd-run --on-active=3 --unit=board-reboot-now systemctl reboot
```

重启后实测：

```
12:51:17  Tasker 已重新拉起（pid=9330）
12:51:43  掉线（豆包进程没了、投屏没了）→ 重新拉起
12:51:47  USB 已经是文件传输模式（mtp,adb）
12:51:48  豆包不在前台，先拉起
12:51:54  「打电话」@(395,2084)
12:51:59  「共享屏幕」@(413,2117)
12:52:02  点「立即开始」授权
12:52:03  系统确认投屏会话：owner com.larus.nova
12:52:06  画面确认：通过
12:52:08  已按 HOME 回桌面，投屏仍在
```

**开机到投屏成功 25 秒，无人干预。**

### 坑：/tmp 重启会被清空

自检脚本最初放 `/tmp/postboot_check.sh`，重启后单元直接
`status=203/EXEC`。已改到 `scripts/postboot_check.sh`。

### USB 用途框：根因与两层处理

根因是 `UsbModeChooserReceiver` 对五个广播都 `startActivity`：

```
UsbModeChooserReceiver: action = android.hardware.usb.action.USB_STATE
UsbModeChooserReceiver: action = android.settings.action.MEDIA_MTP_TRANSFER_FAILED
START u0 {cmp=com.android.settings/.Settings$UsbDetailsActivity}
```

处理：

```bash
# 只禁这一个组件，不动 com.android.settings 本体
adb -s 892e20c9 shell 'su -c "pm disable --user 0 \
  com.android.settings/.connecteddevice.usb.UsbModeChooserReceiver"'
# → Component {...} new state: disabled
```

**恢复原行为**：

```bash
adb -s 892e20c9 shell 'su -c "pm enable --user 0 \
  com.android.settings/.connecteddevice.usb.UsbModeChooserReceiver"'
```

注意：该系统**没有** `pm get-component-enabled-state`（`Unknown command`），
无法查询状态；`pm disable` 是幂等的，所以守护每次启动无条件执行一次即可。

### boot_id 自愈（手机重启后自动补禁）

不做定时 `su` 复查（费电），改读 `/proc/sys/kernel/random/boot_id`（普通权限可读）：

- 与 `state/phone_boot_id` 不一致 → 判定手机重启过 → 重新 disable 一次
- 一致 → 什么都不做

实测模拟：把记录改成 `00000000-dead-beef-dead-000000000000` 后，
12:59:16 自动检测并重新 disable，记录恢复真实值。

### Tasker 防杀：结论是「不刷模块」

已查实并排除的手段：

| 手段 | 现状 | 结果 |
|---|---|---|
| 电池优化白名单 | 已加入 | 仍被杀 |
| powerkeeper `bgControl` | 已是 `noRestrict` | 仍被杀 |
| `manual_list.white_list`（自启动白名单） | 整表为空 | 无内容可加 |
| Magisk 防杀模块 | 无可信项目 | 不刷，风险大且升级易失效 |

**采用板子侧看门狗**，不动手机系统。实测 `am force-stop` 后 **12 秒**复活。

### 又一次真实自愈（非模拟）

```
12:59:20  Tasker 不在（被 MIUI 杀了），重新拉起
12:59:28  Tasker 已重新拉起（pid=11616）        # 8 秒
12:59:40  已拉起豆包
12:59:43  「打电话」@(395,2084)
12:59:48  「共享屏幕」@(413,2117)
12:59:51  点「立即开始」授权 + 系统确认投屏会话
12:59:55  画面确认：通过
12:59:56  已按 HOME 回桌面，投屏仍在            # 共 18 秒
```

### 状态文件读取注意

重拉成功后，`state/keepcast.json` 里的 `pid` 要**等下一轮主循环**才刷新
（实测滞后约 20 秒，显示旧 pid 而实际已换新）。不是 bug。

### 已验证 / 未验证

- ✅ 板子重启后全链路自动恢复（真实重启）
- ✅ 手机侧的 receiver 禁用挺过板子重启
- ✅ 手机重启后自动补禁（boot_id 模拟验证）
- ✅ 豆包被杀、Tasker 被杀均自愈
- ⚠️ 手机**真的重启**后 `package-restrictions.xml` 是否被 MIUI 重置，
  无真机依据 —— 但 boot_id 机制会在它插回来后第一轮自动补上
