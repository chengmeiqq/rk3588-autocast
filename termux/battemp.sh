#!/system/bin/sh
# battemp.sh - 伪造电池温度上报，屏蔽假电池的 -40.6C 哨兵值
#
# 背景：这台手机换了假电池 + 直通电源，假电池没有温度传感器。
# 驱动在「读不到」时返回 -406（-40.6C 的哨兵值），而且会在
# 450（45.0C 兜底值）和 -406 之间反复跳。
#
# 内核节点 /sys/class/power_supply/battery/temp 是 -r--r--r--，
# 来自 qcom,battery_charger 驱动，写不进去，所以不能改内核值。
#
# 但 framework 的 BatteryService 支持覆盖上报值：
#   dumpsys battery set temp <0.1C 为单位>
# 覆盖后即使内核跳成 -406，framework 仍报设定值。
# 实测：内核 -406 时 framework 稳守 300。
#
# 不覆盖的后果（实测）：MIUI 认为电池 -40C → health=COLD →
#   PowerKeeper.Thermal(-40) → ChargerBatteryIntelligenceReceiver
#   → PowerNoticeUI 弹「低温关机提醒」，反复弹。
# settings 里的 pc_low_temperature_extreme_endurance_switch 管的是
# 极限续航模式，跟这个弹窗无关，关它没用（已验证）。
#
# 注意：`dumpsys battery set` 是临时覆盖，重启就丢，
# 所以由本脚本在开机后重新设置。
# 恢复真实上报：dumpsys battery reset

TAG=battemp
LOG=/data/local/tmp/battemp.log
TEMP=${NOVA_BATTEMP:-300}   # 300 = 30.0C，中立值

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; }

# 等系统就绪：太早 dumpsys battery set 会被 BatteryService 初始化覆盖掉
i=0
while [ $i -lt 120 ]; do
  [ "$(getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 2
  i=$((i + 1))
done
# boot_completed=1 之后 BatteryService 还要一会儿才把状态广播出来
sleep 10

log "启动 pid=$$ 目标温度=${TEMP}"

# 每 60s 重申一次。正常情况下设一次就够，但：
#  - 拔插电源、开关飞行模式可能让 BatteryService 重建状态
#  - 别的东西执行 dumpsys battery reset 会清掉覆盖
# 重申一次的成本可以忽略，所以定期兜底。
#
# 同时盯住：如果 framework 报的温度变了（覆盖被别人清了），立刻补回来。
while true; do
  cur=$(dumpsys battery 2>/dev/null | sed -n 's/^ *temperature: *//p' | tr -d '\r\n')
  if [ "$cur" != "$TEMP" ]; then
    dumpsys battery set temp "$TEMP" >/dev/null 2>&1
    new=$(dumpsys battery 2>/dev/null | sed -n 's/^ *temperature: *//p' | tr -d '\r\n')
    log "温度 $cur -> $new（$*）"
  fi
  sleep 60
done
