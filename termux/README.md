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
./nova.sh once      # 投屏 + 直播（豆包后台投屏，直播前台）
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

## ✅ 投屏和直播可以同时跑（之前结论错了）

我以前写「投屏与直播互斥、无法绕过」——**错的**。不需要在两者之间选。

正确顺序：

1. 先在豆包里开屏幕共享（这时豆包在前台）
2. **再把直播 App 拉前台**

豆包的屏幕共享跑在 `ScreenCaptureService` 这个**前台服务**里，
切到后台不影响；而直播 App 占了前台就能拿到相机。
于是豆包看得到直播画面，直播也不断流。

实测（独立复核，不靠脚本自报）：

```
前台:        com.opencode.multilensipcam/.MainActivity
ScreenCaptureService  isForeground=true  uidState: TOP
Camera client:  com.opencode.multilensipcam (PID 22728)
帧:  571020B HTTP 200 / 570104B / 571990B
参数:  3840x2160 q100 zoom1.53
```

也解释了早前那个没复现的 `owner=com.larus.nova` 且 `streaming=True`、
1.51MB 的并存状态——那不是偶然，是正常行为。

### 之前为什么误判成互斥

两处错误叠在一起：

**错误一：检测方法。** 我用 `uiautomator dump` 找「退出屏幕共享」。
但 dump 只包含**当前前台 App** 的界面，豆包一进后台，它的 UI 就不在树里，
于是误报「没投屏」。然后用 `dumpsys media_projection` 兜底——它一直返回 `null`，
也不可用。

**正确信号**是豆包的前台服务：

```
dumpsys activity services com.larus.nova | grep ScreenCaptureService
  → isForeground=true
```

投屏中计数 3，退出后 0，**与哪个 App 占前台无关**。

**错误二：我自己把直播杀了。** `ensure_share` 里写了

```bash
am force-stop com.opencode.multilensipcam   # 为了让豆包「占住相机」
```

结果直播 App 一死 API 就没了，看起来就像「被挤掉了」。
已删掉这行。豆包开屏幕共享并不需要独占相机。

### 分屏是多余的

我试过 `--windowingMode 3` 想让两个 App 都可见，结果 `23B HTTP 503`，
投屏也断了。不需要分屏——豆包在后台就行。

## 开机自启：用 Magisk service.d

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

不要了。投屏 + 直播**同时**跑这一点已经验证通过，全部在手机内闭环。
板子侧原有的 `keepcast` / `lenscast` / `smartcast`
现在反而可能与手机上的守护抢前台，建议保持暂停（`state/allow.json` 里已置 false）。

## 重启验证（已实测通过）

`adb reboot` 后全程无人工干预，手机自己恢复：

```
13:22:04  Magisk 服务启动 pid=2340
21:22:11  系统就绪（等 sys.boot_completed=1）
21:22:19  守护不在，拉起
21:22:19  守护启动
21:22:30  点「打电话」@ 395 2084
21:22:41  点「共享屏幕」@ 413 2117
21:22:47  点「立即开始」@ 859 1515
21:22:51  投屏已开
21:22:51  直播拉前台
21:23:02  并存 OK：投屏在，直播 569132B
```

重启后 96 秒的独立复核：

```
进程链:  root 2340  /data/adb/service.d/nova.sh
         root 12398  └─ bash ./nova.sh watch
前台:    com.opencode.multilensipcam/.MainActivity
共享:    ScreenCaptureService isForeground=true
相机:    Client Package Name: com.opencode.multilensipcam
帧:      574351B / 575089B / 572566B   全 HTTP 200
参数:    streaming=True 3840x2160 q100 zoom1.53
```

注意 `persist.adb.tcp.port=5555` 也保住了，但那已经不重要——
整套流程不再依赖 adb。

### 重启后手工恢复（万一）

```sh
# 手机内，Termux
cd ~ && ./nova.sh status      # 看状态
cd ~ && ./nova.sh once        # 手动跑一次
cd ~ && ./nova.sh watch &     # 手动起守护
```

Magisk 服务每分钟自检，守护死了会自己重拉，通常不用管。
