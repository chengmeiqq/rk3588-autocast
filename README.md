# rk3588-autocast

RK3588 板子 + Android 手机：**通电即自动投屏**，无需人工干预。

给板子通电 → 手机通过 USB 自动开机 → 等 Android 就绪 → 点开豆包 App →
点「打电话」→ 点「开启投屏」→ 确认系统授权 → 回桌面。全程约 35 秒。

用 [Airtest](https://github.com/AirtestProject/Airtest) 做图像识别点击，
Flask 提供 web 控制面板，systemd 负责开机自启。

## 架构（2026-11-13 实测准入）

三个常驻 systemd 服务，全部 `Restart=always`，均已 `enable`（开机自启）：

| 服务 | 端口 | 作用 |
|---|---|---|
| `airtest-adbd` | — | 管 adb 通道：USB 优先，掉了自动切 WiFi `192.168.1.119:5555` |
| `airtest-smartcast` | 8130 | 窄 HTTP 接口：`POST /cast`、`/step/*`、`/status` |
| `airtest-keepcast` | — | **投屏守护**：掉线自动重拉 + USB 框监视 + Tasker 存活监视 |

另有一次性 `airtest-postboot-check`（`Type=oneshot`，开机 45 秒后写自检日志到
`logs/postboot_check.log`），用来事后核对开机是否一切正常。

`airtest-usbwatch` 已**停用**（`disabled`）：它会在手机插上时无脑开跑一整轮，
而现在的策略是「插着才守护、有人调用才动作」，见下。

### keepcast 守护的三条线

`keepcast.py` 一个进程里跑一个主循环 + 两个守护线程，共用同一把
`/tmp/_cast.lock`（和 Tasker 的 `/cast` 也共用，谁先到谁跑，另一个当场让路）：

| 线 | 间隔 | 做什么 |
|---|---|---|
| 主循环 | 20s | 双判据查健康（`casting_owner` 非空 **且** `pidof com.larus.nova`）；不健康就抢锁重拉整轮 |
| `usb_dialog_watch` | 2.5s | 盯「USB 用于」框，在前台就点「传输文件」 |
| `tasker_watch` | 20s | `pidof net.dinglisch.android.taskerm` 为空就 `am start` 拉起 |

三条线都先过同一道门：`state/keepcast.pause` 存在 → 不动手；
USB 设备节点不在（读 sysfs，不发 adb）→ 待命。**拔了线就完全不碰手机。**

### 为什么是「双判据」而不是看进程

只看 `pidof` 会漏「进程在但投屏被掐」——这种情况真实发生过；
只看 `casting_owner` 会漏「会话残留但进程已死」。两个都成立才算健康。

### USB 用途框为什么老弹（已从根上关掉）

MIUI 的 `com.android.settings/.connecteddevice.usb.UsbModeChooserReceiver`
注册了 `USB_STATE` / `USB_DEVICE_ATTACHED` / `USB_DEVICE_DETACHED` /
`MEDIA_MTP_TRANSFER_FAILED` / `BOOT_COMPLETED` 五个广播，
**每收到一个就 `startActivity` 把 `Settings$UsbDetailsActivity` 拉出来**。
投屏健康时没人管它，框就杵在屏幕上挡住后续所有点击。

处理分两层：

1. **根上**：`pm disable --user 0 ...UsbModeChooserReceiver`（只禁这一个组件，
   不动 `com.android.settings` 本体，设置功能正常）。
2. **兜底**：万一被系统升级重置，`usb_dialog_watch` 仍会 2.5s 内点掉。

禁用状态存在 `/data/system/users/0/package-restrictions.xml` 的
`disabledComponents`，板子重启后仍在。手机自己重启会不会被 MIUI 重置**无法预判**，
所以用 `boot_id` 判断：读 `/proc/sys/kernel/random/boot_id` 与
`state/phone_boot_id` 比对，变了才重新禁一次（平时零开销，不 `su`）。

### Tasker 也会被 MIUI 杀

实证：`SmartPower` 在 Tasker `bgControl=noRestrict`、且在电池优化白名单里的情况下
照样把它杀了，死了 17 分钟没人发现。`manual_list` 的 `white_list` 整张表是空的。
没有可靠口碑的「防 MIUI 杀进程」Magisk 模块，刷模块风险大且 MIUI 升级易失效。

**所以走板子侧看门狗**（`tasker_watch`），不动手机系统。实测 `am force-stop`
后 12 秒复活。

## 为什么需要这个

本项目的难点不在"点按钮"，而在**无人值守**：

| 问题 | 处理方式 |
|---|---|
| 手机是关机的，USB 上电才开始启动 | 等 `sys.boot_completed=1`，不是看到 USB 节点就跑 |
| 板子开机时手机已插着，等不到"拔→插"跳变 | 启动时检测到插着就立刻跑一次 |
| 板子没有 RTC 电池，冷启动时间掉回 2021 | 开机先用 HTTP `Date` 头校时，再 NTP 精调 |
| 校时跳变会让超时误判 | 所有计时用 `time.monotonic()`，不用 `time.time()` |
| adb 半死连接会让服务永久卡住 | subprocess 自带超时 + 通道自动切换 |

## 结构

```
airweb.py        Flask 服务：流程执行 + 截图 + 状态（端口 8123）
adbd.py          adb 通道管理：USB 优先，断了切 WiFi
keepcast.py      投屏守护：主循环 + USB 框监视 + Tasker 存活监视
scripts/postboot_check.sh  开机后自检（写 logs/postboot_check.log）
airlib.py        Airtest 封装：连接、找模板、点击、等待
usbwatch.py      盯 USB 口（读 /sys，不依赖 adb/网络），插上就跑
keytrigger.py    音量键触发（按两下音量减），手动兜底
autowatch.py     另一个触发器（备用）
airctl           命令行工具
ui.html          控制面板
timesync.sh      开机校时
start-all.sh     按序拉起全部服务
stop-all.sh      干净停掉
flows/           流程定义（JSON，7 步）
templates/       图像识别模板
macros/          演示素材
```

## 快速开始

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 启动
./start-all.sh
# 面板： http://<板子IP>:8123
```

手动跑一次流程：

```bash
curl -X POST http://127.0.0.1:8123/flow/run \
     -H 'Content-Type: application/json' \
     -d "{\"flow\":\"flows/通话投屏.json\"}"
```

## 开机自启

需要两个 systemd 单元（见 `RUN.md`）：

- `airtest-timesync.service` — 校时，`Before=airtest-autocast.service`
- `airtest-autocast.service` — 拉起 airweb / usbwatch / keytrigger

```bash
sudo systemctl enable --now airtest-timesync airtest-autocast
```

## 流程定义

`flows/通话投屏.json` 是 7 步，每步都带验证（`expect_text` /
`expect_not_text` / `expect_casting`），失败会回退到最近的落脚点重试：

1. `wake` 唤醒屏幕
2. `dismiss` 关掉 MIUI 的「USB 用于」弹窗
3. 启动 `com.larus.nova`（豆包）
4. 点「打电话」
5. 点「开启投屏」
6. 点「立即开始」（系统授权窗）
7. `home` 回桌面，确认投屏仍存活

## 已验证

真实断电重启（拔 USB + 板子断电）后：开机自启 → 等手机开机 → 7/7 通过，
`dumpsys display` 确认 `owner com.larus.nova`。

详见 [`RUN.md`](RUN.md)，里面有完整的实测日志、踩坑记录和设计取舍。

## 授权

代码 MIT。`templates/` 里的截图是 App 界面，仅用于图像匹配。
