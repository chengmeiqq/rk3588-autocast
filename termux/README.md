# termux/ —— 手机独立运行，不需要板子

**结论：这台手机的 Termux 能拿到 root，所以整套流程可以完全在手机内跑完，RK3588S 可以拔掉。**

## 关键发现：Termux 有 root

```
$ run-as com.termux sh -c 'su -c id'
uid=0(root) gid=0(root) groups=0(root) context=u:r:magisk:s0
```

之前反复卡住的三件事，一次全通：

| 操作 | 无 root 时 | 有 root 后 |
|---|---|---|
| `input keyevent` | `SecurityException: needs INJECT_EVENTS` | ✅ `rc=0` |
| `am start` | `package=com.android.shell does not belong to uid=10243` | ✅ 正常 |
| `setprop persist.adb.tcp.port` | `rc=1` | ✅ 写入成功，重启仍有效 |

所以**不需要回环 adb，不需要板子执行 `adb tcpip`**，也就没有了「重启后回环失效」的问题。

## 直播为什么连 adb 都不需要

直播 App 的 HTTP API 就在 `127.0.0.1:41737`，Termux 直接 `curl` 即可：

```
$ curl -s http://127.0.0.1:41737/api/state
{"streaming":true,"selectedResolution":"3840x2160","qualityValue":100,...}

$ curl -s -X POST "http://127.0.0.1:41737/api/control?resolution=3840x2160&quality=100&zoom=1.53"
{"ok":true}
```

参数控制、开流、抓帧全走这个 API，**一次 adb 都不用调**。

## 文件

- `nova.sh` —— 主脚本，手机独立版（当前用这个）
- `nova_agent.sh` —— 旧版，依赖回环 adb，留作对照
- `find_ui.py` —— 旧版 UI 查找，逻辑已并入 `nova.sh`
- `boot.sh` —— 旧版入口（回环路线）

用法：

```sh
./nova.sh once      # 投屏 + 直播各一次
./nova.sh lens      # 只保直播（参数 + 出图）
./nova.sh share     # 只做投屏
./nova.sh watch     # 常驻守护，20s 一轮
./nova.sh status    # 看状态
./nova.sh install   # 装开机自启
./nova.sh stop      # 停守护
```

## 三个必须踩过的坑

### 1. 豆包没有 `.MainActivity`

```
$ am start -n com.larus.nova/.MainActivity
Error: Activity class {com.larus.nova/com.larus.nova.MainActivity} does not exist.
```

真正的入口是启动别名：

```
$ cmd package resolve-activity --brief com.larus.nova
com.larus.nova/com.larus.home.impl.alias.AliasActivity1
```

### 2. 投屏状态不能查 `dumpsys`

- `dumpsys activity` 里没有「正在共享屏幕」这串字
- `dumpsys media_projection` 返回 `null` 也不代表没开（实测过）

唯一可靠来源是 UI 树：dump 一次，看有没有「退出屏幕共享」按钮。

### 3. `find_ui` 必须完全相等匹配

投屏后的屏幕上有：

```
text="正在共享屏幕"       ← 标题，bounds [375,767][705,841]     y≈804
content-desc="共享屏幕"   ← 真按钮，bounds [308,2012][518,2222]  y≈2117
```

用 `grep -F "共享屏幕"` 做子串匹配会**先命中标题**，点下去毫无反应，
日志却写「已点共享屏幕」——比直接报错更难查。必须完全相等，且优先 `clickable=true`。

## ⚠ 硬限制：投屏和直播互斥，无法绕过

MIUI 只允许**可见前台**的 App 用相机。实测对照：

| 前台 | `/snapshot.jpg` |
|---|---|
| 豆包通话页 | `0B / HTTP 000` |
| 直播 App（在后台） | `23B / HTTP 503` |
| 直播 App（干净前台） | `560671B / HTTP 200` ✅ |

**试过分屏（`--windowingMode 3`）：结果是 `23B / HTTP 503`，投屏也断了。**
两个 App 同时可见也救不了，这是相机独占，不是窗口问题。

所以 `watch` 默认走 `lens` 模式：检测到投屏在开就主动让路，不抢前台。

⚠ 要提一点：实测中一度出现过 `owner=com.larus.nova` 且 `streaming=True`、
帧约 1.51MB 的并存状态，之后没能复现。默认按互斥处理，因为那是可复现的常态。

## 开机自启：用 Magisk service.d

`Termux:Boot` 装不上（F-Droid 版签名与已装 Termux 冲突，
`INSTALL_FAILED_SHARED_USER_INCOMPATIBLE`）。而且 Termux 本身是普通 app，
会被 MIUI 的 SmartPower 杀。

所以用 Magisk 的 `/data/adb/service.d/`：

- 里面跑的是 **root shell**，不是 app 进程，SmartPower 碰不到
- 开机自动执行所有 `.sh`，不依赖 Termux app 是否被拉起
- 已有的 `/data/adb/service.d/taskerd.sh`（Tasker 存活守护）用的是同一招

`nova.sh install` 会写 `/data/adb/service.d/nova.sh`，它负责：
1. 等 `sys.boot_completed=1`（不等的话 `am start` 会失败）
2. 每 60s 检查 `nova.sh watch` 是否活着，不在就重拉
3. `setprop persist.adb.tcp.port 5555`（重启后仍有效，虽然现在不需要了）

## 板子还要不要

不要了。除了「投屏 + 直播同时进行」这一件事做不到（系统限制），
其余全部在手机内闭环。板子侧原有的 `keepcast` / `lenscast` / `smartcast`
现在反而可能与手机上的守护抢前台，建议保持暂停（`state/allow.json` 里已置 false）。
