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

## 三步验证（3 轮实测）

`test3.sh` 从干净状态（投屏已关）起跑，每轮逐步核对信号。连跑 3 轮：

| 步骤 | 验证信号 | 1 | 2 | 3 |
|---|---|---|---|---|
| 1 豆包在前台 | `topResumedActivity` 含 `com.larus.nova` | ok | ok | ok |
| 2 通话页 | 前台 = `RealtimeCallActivity` | ok | ok | ok |
| 3 点共享屏幕 | 找到按钮 @413 2117 | ok | ok | ok |
| 4 授权弹窗 | 弹了就点「立即开始」 | skip | skip | skip |
| 5 投屏真的开了 | `ScreenCaptureService` 计数 > 0 | ok | ok | ok |
| 6 直播在前台 | 前台 = `com.opencode.multilensipcam` | ok | ok | ok |
| 7 参数正确 | 解析 JSON 比对三项 | ok | ok | ok |
| 8 出图 | 帧 > 10KB 且 HTTP 200 | ok | ok | ok |
| 9 并存 | 5 和 6 同时成立 | ok | ok | ok |

第 4 步 3 轮都是 `skip`：**系统授权弹窗只在首次或授权被撤销时出现**，
平时点「共享屏幕」直接就开了。脚本两条路都处理。

出图字节数：482957 / 484469 / 484217，波动很小，说明是稳定流。

## 之前哪几步没有验证

第一版能跑，但**四个点击全都没有验证，只靠固定 sleep**，所以会偶发失败。
这是被 3 轮测试打出来的：

**问题一：弹窗会盖住整个 UI 树。**

`uiautomator dump` 只拍顶层窗口。系统投屏授权弹窗
（`com.android.systemui/.media.MediaProjectionPermissionActivity`）
一弹出来，dump 里就只剩弹窗，通话页的「共享屏幕」按钮消失了。
于是 `find_ui "共享屏幕"` 返回空 → 报「没有共享屏幕按钮」→ 但其实只差点了「立即开始」。

弹窗长这样（实测）：

```
'要开始使用豆包录制或投射内容吗？'      click=false @540,1086
'取消'                              click=true  @184,1515
'立即开始'                            click=true  @859,1515
```

**问题二：不能拿「立即开始」当通话页判据。**

我原先写 `has_ui "立即开始"` 就算通话页起来了。可是这个文字
**弹窗上也有**。第 2 轮就是这样误报 ok，实际前台是直播 App。
现在只认 `RealtimeCallActivity`。

**问题三：「投屏没开」其实只是等得不够。**

第 1 轮点了共享屏幕后 10 秒判定失败，第 2 轮同一位置却读到
`ScreenCaptureService=3`——投屏在我判定之后才起来。系统弹窗到按钮
可点之间要几秒。现在改成**状态机轮询**（最多 12 轮 × 2–3 秒），
不是「睡够 X 秒就假定成功」。

## 现在的 ensure_share 逻辑

不再按固定顺序 sleep，而是循环问「投屏开了吗 / 弹窗在吗」：

```
loop 最多 12 次:
    ScreenCaptureService > 0  →  成功返回
    有「立即开始」            →  点它，继续
    还没点过「共享屏幕」       →  找按钮点它，继续
    否则                     →  等 2s
```

哪条路先满足都行：无弹窗直开、或者弹窗→点→开。

## 每步的验证强度

| 动作 | 验证方式 |
|---|---|
| 拉起豆包 | 前台 Activity 含包名（不是 grep dumpsys，后台 task 也会命中） |
| 点打电话 | 前台变成 `RealtimeCallActivity`，最多等 20s + 重试 1 次 |
| 点共享屏幕 | 点完不假设成功，交给下面的轮询判 |
| 点立即开始 | 轮询里发现才点，点完继续轮询 |
| 投屏成立 | `ScreenCaptureService` 计数 > 0（唯一判据） |
| 直播前台 | 前台 Activity 含包名，最多重试 4 次 |
| 参数 | 读回 `/api/state`，比对分辨率/质量/变焦三项 |
| 出图 | `snapshot.jpg` 字节数 > 10KB 且 HTTP 200 |
| 并存 | 投屏信号 + 前台 + 出图三者同时成立 |

## 断电上电（直供电场景）实测

用户场景：手机改了直供电，**断电就关机，插电就开机**。
要求每次上电都自动进到「豆包后台投屏 + 直播前台」的状态，无人干预。

这跟普通重启不一样，必须额外处理四件事：

### 1. 等网络就绪（否则必失败）

断电后 WiFi 要几十秒才关联上，而豆包打电话需要联网。
原来 `ensure_network` 只关飞行模式，**不等到真能出网**就往下走。

现在加了 `wait_online`：

```sh
# 先等 wlan0 拿到 IP（最多 60s）
# 再用 MIUI 的 204 端点确认真能出网（最多 60s）
curl -s -m 5 -o /dev/null -w '%{http_code}' \
  http://connect.rom.miui.com/generate_204   # 要 204
```

日志里会看到 `网络就绪（192.168.1.119）`。

### 2. boot_completed=1 不等于能操作

MIUI 在 `sys.boot_completed=1` 之后还要几十秒初始化包管理器、
通知、输入服务。太早 `am start` 会失败。
service.d 里加了固定延迟：

```sh
wait_boot || log "等 boot_completed 超时，继续"
log "系统就绪"
sleep 15                      # 给 MIUI 收尾
log "延迟 15s 后开始"
```

### 3. set -u 下变量必须先初始化

单实例锁里写了 `local d pid found`，循环没命中时 `found` 从未赋值。
`set -u` 下一引用就 `found: unbound variable` 直接退出。
**守护反复崩溃、service.d 每 60s 重拉**，看起来像「起不来」。

修法：`local d pid cmd found=""`。

### 4. 扫 /proc 要先判可读

`for d in /proc/[0-9]*` 循环里进程随时会退出，读 cmdline 会刷：

```
./nova.sh: line 531: /proc/12731/cmdline: No such file or directory
```

修法：`[ -r "$d/cmdline" ] || continue`，并把读取整体丢 stderr。

### 实测时间线（21:48:36 上电 → 21:49:49 就绪，73 秒）

```
21:48:36  service.d 启动 pid=2269
21:48:50  系统就绪（boot_completed=1）
21:49:05  延迟 15s 后开始
21:49:07  守护不在，拉起
21:49:15  守护启动
21:49:15  网络就绪（192.168.1.119）   ← wait_online 生效
21:49:15  拉起豆包
21:49:25  点「打电话」
21:49:27  通话页已起
21:49:32  点「共享屏幕」
21:49:35  点「立即开始」
21:49:38  投屏已开
21:49:38  直播拉前台
21:49:49  并存 OK：投屏在，直播 480188B
```

上电后 124 秒的独立复核（不靠脚本自报）：

```
uptime:  124s
前台:    com.opencode.multilensipcam/.MainActivity
投屏:    ScreenCaptureService 3 次
相机:    Client Package Name: com.opencode.multilensipcam
帧:      1299918B / 1267212B / 1269463B   全 HTTP 200
参数:    streaming=True 3840x2160 q100 zoom1.53
网络:    inet 192.168.1.119
屏幕:    Awake
```

帧涨到 1.27MB（之前 480KB）是因为重启后画面内容更复杂，
不是分辨率变化——参数仍是 3840x2160。

### 已确认的配套设置

| 设置 | 值 | 为什么重要 |
|---|---|---|
| `stay_on_while_plugged_in` | `7` | 插着电就不息屏，直供电必须 |
| `lock_settings get-disabled` | `true` | 无锁屏，开机不会卡在密码界面 |
| `screen_off_timeout` | `600000` | 10 分钟，配合上一项 |
| `wifi_on` | `1` | 开机自动开 WiFi |
| `wifi_scan_always_enabled` | `1` | 息屏也扫，加快关联 |
| `ro.crypto.state` | `encrypted`（file） | 用户锁屏已禁用，CE 存储可用 |

## 巡检优化（20s 间隔 + 不再每轮吃掉 500KB）

用户要求把 service.d 的巡检从 60s 改成 20s。改的过程中发现原来的巡检
**每一轮都在下载一整帧 4K 图**（约 500KB），20s 一轮等于持续占用带宽和 CPU。
顺手改掉了。

### 实测数据

| 方式 | 耗时 | 流量 |
|---|---|---|
| `GET /snapshot.jpg`（原方案） | 358 ms | 498 KB |
| `HEAD -L /snapshot.jpg`（现方案） | 51 ms | **0 B** |

### 两个关键事实

1. **这个服务端不支持 Range。** 实测 `curl -r 0-2047` 仍然返回完整的
   498KB 帧，所以「只取前 2KB」这条路走不通。
2. **快照是 302 跳转的。** `/snapshot.jpg` → `/snapshots/<ts>.jpg`。
   不带 `-L` 会拿到 `302 0B`。带 `-L` 才是 `200`。

所以用 `HEAD -L`：跟到最终图，只读响应头，下 0 字节。

### 改了什么

- `frame_alive()`：`GET` 改 `HEAD -L`，只判 HTTP 200。
  「有没有画面」交给 `api/state` 的 `streaming` 判断，不靠下载图。
- `lens_probe()`：新增。一次 `api/state` 调用回答全部问题
  （streaming / 参数是否已对 / 当前分辨率质量变焦），
  替代原来一轮里跑 3 次 `api_state` + 2 次 `lens_params_ok`。
- `ensure_lens()`：
  - 参数已经对就**完全不写日志**（原来是每轮刷「参数已对」）。
  - `streaming=true` 时不再重复 `POST /api/control?streaming=true`。
  - 帧异常时才真的抓一整帧看大小，用于诊断。
- `mode_both()`：并存确认也改用 `frame_alive`。

### 效果

一轮巡检 2.5–2.7 秒（连跑 3 次：2619 / 2699 / 2535 ms），
正常时日志只有 2 行，不再每 20 秒写一次「参数已对」。

### 心跳

静默有代价：日志不涨就看不出守护还活着。所以 `watch` 循环每
15 轮（20s × 15 = 5 分钟）写一行心跳：

```
心跳 投屏=1 直播=1 3840x2160 rc=0
```

心跳只读状态，不下载帧。

## 单实例锁：三个自己坑自己的 bug

把巡检改成 20s 之后暴露了一串问题，根因都是**探测进程把自己算进去了**。
这些 bug 只在「两个守护并存」或「守护被反复重拉」时才显形，症状是
**直播 API 端口直接不监听了**（App 进程还在、还是前台，但 41737 没了）。

### 现象

2026-11-15 22:04，投屏突然掉、重跑流程、`直播 API 不通`、
`HTTP 000`。查 `ps` App 还活着、还是 `topResumedActivity`、
`dumpsys media.camera` 相机也在它手里 —— 但 `/proc/net/tcp` 里没有 41737。

同时在跑的守护进程有 **两个**：`16316`（service.d 起的，root）
和 `27190`（手动起的，u0_a243）。两个每 20s 抢一次投屏和开流，
把 App 的 HTTP 服务打死了。

### bug 1：不同 uid 互相看不见

`acquire_lock` 原本只是「扫 /proc 找 nova.sh watch」。但：

- service.d 起的守护是 **root**
- 从 Termux 手动起的是 **u0_a243**
- u0_a243 扫不到 root 的进程（`/proc/<root_pid>` 读不了）

于是 Termux 起的守护认为「没有别人在跑」，直接起来。

**修法：** 扫描统一走 `R`（→ `su -c`），用 root 身份扫全部进程。

### bug 2：`stop` 杀不掉 root 守护

原来 `stop` 是 `kill $(cat $PIDF)` 或 `pkill -f "nova.sh watch"`。
u0_a243 的 `pkill` 碰不到 root 进程，于是「已停」只是假象，
老的还在跑，新的又起来。

**修法：** `stop` 也走 `R "kill -9 $pid"`。

### bug 3（最坑）：探测命令匹配到自己

`find_watch_pids` 用 `*nova.sh*watch*` 子串匹配。但**扫描命令自己的
cmdline 里就带着 `nova.sh` 和 `watch` 这两个词**，于是它匹配到自己。

后果不是「多打印几行」：

- `acquire_lock` 永远认为「已经有守护在跑」→ **永远拒绝启动**
- 日志里刷 `已有守护在跑（3276 3277）`，但 `ps` 里一个都没有

第一版修法：改成严格三段式（参数正好 3 个，第 2、3 个是
`./nova.sh watch`）。**但这还不够** ——

因为 `find_watch_pids` 是 `R` 调用的，`R` 在非 root 下走 `su -c`，
`su` 会 fork 一个 `sh -c`，那条命令行正好是：

```
sh -c for d in /proc/... [ "$2" = "./nova.sh" ] [ "$3" = "watch" ] ...
```

**参数形态和真的守护一模一样**，严格匹配照样拦不住。

**最终修法：** 除了严格匹配，还要**按进程链排除自己**。
从左往右读 ppid，把 `$$` 及其所有祖宗的 pid 收集起来，
扫描时全部跳过。这样无论探测命令长什么样都不会匹配到自己。

### bug 4：service.d 模板同样的错

`nova.sh install` 写进 `/data/adb/service.d/nova.sh` 的模板里，
有两处也是子串匹配：

1. **启动自检**（第 22 行）：`case "$c" in *nova.sh*watch*) ... exit 0`
   —— 这个更致命，开机时如果匹配到自己，service.d 直接退出，
   等于**开机自启彻底失效**。
2. **巡检循环**：同样的子串匹配，会永远认为「守护还在」，真死了也不重拉。

两处都改成严格三段式。

### bug 5：锁文件放错地方

一开始把 flock 锁放 `/data/local/tmp/nova-watch.lock`。
实测这个目录是 `drwxrwx--x shell shell` —— **Termux 用户没有写权限**，
`exec 9>"$LOCKF"` 直接失败。更糟的是原代码忽略了失败，于是 `flock`
作用在一个没打开的 fd 上，**等于没锁**，三个并发启动全放行。

**修法：**
- 锁文件放 `$HOME/.nova/watch.lock`（这个目录本来就是 `0777`，
  root 和 Termux 用户都能写）
- 打开锁文件失败时**直接拒绝启动**，不静默放行

### 为什么用 flock 而不是 mkdir 锁

mkdir 锁需要「判断持锁者是否还活着」，而 pid 会复用、pid 文件可能
残留 —— 判错就等于两个守护并存。flock 由内核管，进程一死锁自动
释放，没有这个歧义。

### 验证

| 场景 | 结果 |
|---|---|
| 同时起 3 个 | 1 个活，2 个被拒（`已有守护持锁，不重复起`） |
| 真实重启（22:16:17 上电） | 76s 全链路 OK，只有 1 个守护 |
| 冷启动后守护数 | 1 个（`16252`） |
| 投屏 / 相机 / 网络 | 3 次 / 直播 App / 192.168.1.119 |
| 帧 | 726888 / 727187 / 725808 B，全 HTTP 200 |
| 参数 | streaming=True 3840x2160 q100 zoom1.53 fps30 |
