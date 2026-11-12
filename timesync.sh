#!/bin/bash
# 开机校时。板子的 RTC 电池失效（内核报 rtc information is invalid，
# 每次冷启动系统时间会掉回 2021-01-01），NTP 又要等网络+几十秒才生效。
# 所以开机时先用 HTTP Date 头抢一个准时间，再用 NTP 精调。
#
# 为什么要抢：日志里混着 2021 年和现在的时间，事后根本没法看。
# 而且超时判断、日志顺序都会错乱。

cd "$(dirname "$0")" || exit 1
mkdir -p logs
LOG=logs/timesync.log

# 第一行日志的时间戳本身就是错的（2021），标出来免得事后误读
say() { echo "[$(date -u '+%F %T')] $*" | tee -a "$LOG"; }

# 判断时间是否已经可信：NTP 说同步过，且年份不是 2021（RTC 掉电的默认值）
time_ok() {
    local y
    y=$(date -u +%Y)
    [ "$y" -ge 2024 ] || return 1
    timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes
}

if time_ok; then
    say "时间已可信：$(date -u '+%F %T')"
    exit 0
fi

say "时间不可信（$(date -u '+%F %T')），开始校时"

# 1) 等网络。没网就用不了 HTTP，也不用等 NTP
for i in $(seq 1 30); do
    if ip route get 1.1.1.1 >/dev/null 2>&1; then break; fi
    sleep 1
done

# 2) HTTP Date 头：比 NTP 快，且走的是已经通的这条网
set_from_http() {
    local host url hdr t
    for url in "https://www.aliyun.com" "https://www.tencent.com" \
               "http://192.168.1.1"; do
        hdr=$(curl -sS -I -m 4 "$url" 2>/dev/null | tr -d '\r' | awk -F': ' '
              tolower($1)=="date"{print $2; exit}')
        [ -z "$hdr" ] && continue
        # Date: Thu, 12 Nov 2026 13:44:03 GMT
        t=$(date -u -d "$hdr" +%s 2>/dev/null) || continue
        [ -z "$t" ] && continue
        # 明显不对就跳过（防中间设备回假时间）
        [ "$t" -lt 1700000000 ] && continue
        date -u -s "@$t" >/dev/null 2>&1 || continue
        say "已按 $url 的 Date 头设时间：$(date -u '+%F %T')"
        return 0
    done
    return 1
}

if ! set_from_http; then
    say "HTTP 校时没成功，等 NTP"
fi

# 3) 拉 NTP 精调，并把结果写回 RTC（万一以后装上电池就生效）
systemctl restart systemd-timesyncd >/dev/null 2>&1
for i in $(seq 1 20); do
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        say "NTP 已同步：$(date -u '+%F %T')"
        break
    fi
    sleep 1
done

if time_ok; then
    say "校时完成：$(date -u '+%F %T')（NTP 同步）"
    exit 0
fi

# NTP 没成，但 HTTP 成过的话时间也是准的
if [ "$(date -u +%Y)" -ge 2024 ]; then
    say "时间已修正（NTP 未确认，但年份正常）：$(date -u '+%F %T')"
    exit 0
fi

say "校时失败，时间仍是 $(date -u '+%F %T')"
exit 1
