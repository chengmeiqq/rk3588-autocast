# rk3588-autocast

RK3588 板子 + Android 手机：**通电即自动投屏**，无需人工干预。

给板子通电 → 手机通过 USB 自动开机 → 等 Android 就绪 → 点开豆包 App →
点「打电话」→ 点「开启投屏」→ 确认系统授权 → 回桌面。全程约 35 秒。

用 [Airtest](https://github.com/AirtestProject/Airtest) 做图像识别点击，
Flask 提供 web 控制面板，systemd 负责开机自启。

## 架构（2026-11-13）

三个独立 systemd 服务，全部 `Restart=always`，崩了自动拉起：

| 服务 | 端口 | 作用 |
|---|---|---|
| `airtest-airweb` | 8123 | 流程执行器，按步执行点击/截图/校验 |
| `airtest-smartcast` | 8130 | Tasker 可调的窄接口：`POST /cast` |
| `airtest-usbwatch` | — | 盯 USB 口，手机插上就开跑；兼投屏看门狗 |

单元文件在 `deploy/`。`airtest-usbwatch` 每 20 秒查一次投屏 owner，
一旦发现投屏消失就自动重跑整轮（豆包通话会被系统清理或挂断，断了必须能自愈）。

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
