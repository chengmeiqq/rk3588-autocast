# termux/ —— 手机本地的启动脚本

在没有 Termux:Boot 的情况下，用手机自己的 Termux 把启动流程固定下来。

## 为什么会有这一套

原来全靠板子远程 `adb shell` 操作手机，三个结构性弱点：

1. **延迟**：板子每 20 秒轮询一次，插上手机后要等一轮才开始。
2. **时序脆**：屏幕 Dozing 时 `dumpsys activity` 返回空，前台判断会误判。
3. **单点**：板子上的守护挂了，手机就没人管了。

把逻辑搬进手机后，`input`/`am`/`dumpsys` 都走本机回环，不受 USB 隧道和轮询间隔影响。

## Termux 的两个权限陷阱（实测）

| 尝试 | 结果 |
|---|---|
| Termux 里直接 `am start` | ❌ `SecurityException: package=com.android.shell does not belong to uid=10243` |
| Termux 里 `input keyevent` | ❌ `SecurityException: Injecting input events requires INJECT_EVENTS` |
| Termux 里 `monkey -p ...` | ❌ `Error: Unable to connect to window manager` |
| Termux 里 `adb connect 127.0.0.1:5555` | ✅ 拿到 **shell 身份**，上面全部可用 |

所以：**一切都通过本机回环 adb 执行，绝不用 Termux 自己的 uid 调系统命令。**

## 文件

- `nova_agent.sh` —— 主脚本。`watch`（守护）/ `lens`（只保直播）/ `share` / `once` / `status` / `stop`
- `find_ui.py` —— 在 uiautomator XML 里按**完全相等**找控件并返回可点坐标
- `boot.sh` —— 外部统一入口：连回环 adb + 起守护（带单实例锁）

## find_ui.py 为什么必须存在

豆包投屏后，屏幕上有：

```
text="正在共享屏幕"              ← 标题，bounds [375,767][705,841]
content-desc="共享屏幕"          ← 真按钮，bounds [308,2012][518,2222]
```

用 `grep -F "共享屏幕"` 做子串匹配，**先命中的是标题**（y≈804），点下去毫无反应，
日志却写「已点共享屏幕」——比直接报错更难查。`find_ui.py` 只认完全相等，
并且优先选 `clickable=true` 的节点。

## ⚠ 硬限制：投屏和直播抢前台，互斥

MIUI 只允许**可见前台**的 App 用相机。实测对照：

| 前台 | `/snapshot.jpg` |
|---|---|
| 豆包通话页 | `0B / HTTP 000`（连不上） |
| 直播 App | `23B / HTTP 503`（连上但相机没工作） |
| 直播 App（干净重启后） | `1181976B / HTTP 200` ✅ |

而且豆包通话页是内部 Activity，`am start` 拉不回来（必须走 App 自己的流程）。
一旦被挤掉，投屏就断了，只能重新点。

**结论：不要试图并发。** 所以 `watch` 默认走 `lens` 模式：
检测到投屏在开就主动让路，不抢前台；投屏关了才接管直播。

## 开机自启（未解决的部分）

- Termux:Boot **装不上**：F-Droid 版签名与已装 Termux 冲突
  （`INSTALL_FAILED_SHARED_USER_INCOMPATIBLE`）。
- `persist.adb.tcp.port` **写不了**：需要 root。
- 手机重启后 adbd 会回到 USB 模式，`127.0.0.1:5555` 随之失效。
  需要板子执行一次 `adb tcpip 5555` 才能恢复回环。

现状：`boot.sh` 可由 Tasker 开机任务或板子调用；回环断了会记录日志并继续重试。
