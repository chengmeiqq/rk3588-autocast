#!/system/bin/sh
# taskerd.sh —— 手机侧 Tasker 存活守护
#
# 为什么需要它：MIUI 的 SmartPower 会杀 Tasker。板子侧 keepcast 已经有一个
# tasker_watch，但那条线只在**插着 USB** 时才干活。手机拿在手里（拔线）的
# 时候，Tasker 死了就没人管。这个脚本补上这个缺口。
#
# 为什么用 Magisk service.d 而不是 Termux：
#   Termux 自己是个普通 app，同样会被 MIUI 杀，需要 Termux:Boot（未装）、
#   tsu（未装）、还要常驻前台通知才勉强活着。service.d 里跑的是 **root
#   shell**，不是 app 进程，MIUI 的 SmartPower 杀的是 app 的进程组，碰不到
#   它。这也是为什么这条路才真正解决问题。
#
# 安装：把本文件放到 /data/adb/service.d/taskerd.sh 并 chmod 755。
# Magisk 开机时会以 root 身份依次执行该目录下所有 .sh。
#
# 日志：/data/local/tmp/taskerd.log（不写 /data/adb，免得混进模块目录）

TAG="taskerd"
PKG="net.dinglisch.android.taskerm"
# Tasker 的主 Activity；.MainActivity/.Main 都不存在，实测只有这个能用
ACT="$PKG/.Tasker"
LOG="/data/local/tmp/taskerd.log"
INTERVAL="${TASKERD_INTERVAL:-15}"

# 收到 TERM/INT 就干净退出。
# 实测：不加 trap 时 kill（SIGTERM）不生效——mksh 会把信号压到当前前台
# 命令（sleep 15）返回后才处理，看起来像"杀不掉"，只能 -9。加了 trap
# 之后把 sleep 改成后台+sleep 组合太重，这里直接让 trap 生效即可：
# mksh 在 trap 设置后会中断 wait 的 sleep 并执行 handler。
trap 'log "收到退出信号，停止看守"; rm -f /data/local/tmp/taskerd.pid; exit 0' TERM INT

# 单实例：已经有一份在跑就退出，避免 service.d 重复执行叠出多个循环。
# 为什么扫 /proc 而不是只信 pid 文件：pid 文件可能被清掉（/data/local/tmp
# 会被系统或用户清理），只信文件就会叠出第二份循环，两份同时 am start
# 反而互相干扰。这里以"有没有别的 taskerd 进程"为准。
me=$$
for d in /proc/[0-9]*; do
    opid=${d#/proc/}
    [ "$opid" = "$me" ] && continue
    cmd=$(cat "$d/cmdline" 2>/dev/null | tr '\0' ' ')
    case "$cmd" in
        *taskerd.sh*)
            echo "$(date '+%F %T') $TAG 已有实例 pid=$opid，本实例退出" >> "$LOG"
            exit 0
            ;;
    esac
done
echo $me > /data/local/tmp/taskerd.pid

log() { echo "$(date '+%F %T') $TAG $*" >> "$LOG"; }

STOP=/data/local/tmp/taskerd.stop

# 分次睡：每秒看一次停止标记。
# 为什么不用「sleep 15 + trap」：mksh 的 trap 要等当前前台命令（sleep）
# 返回才处理，实测 kill（SIGTERM）后 15 秒都没退出，看起来像杀不掉。
# 切成 1 秒一次之后，TERM 最迟 1 秒内就生效，停止标记也最多 1 秒响应。
nap() {
    n=0
    while [ "$n" -lt "$INTERVAL" ]; do
        if [ -f "$STOP" ]; then
            log "发现停止标记 $STOP，退出"
            rm -f "$STOP" /data/local/tmp/taskerd.pid
            exit 0
        fi
        sleep 1
        n=$((n + 1))
    done
}

# 日志别无限长：超过 2000 行就砍到后 1000 行
trim_log() {
    [ -f "$LOG" ] || return 0
    n=$(wc -l < "$LOG" 2>/dev/null || echo 0)
    if [ "$n" -gt 2000 ]; then
        tail -1000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
    fi
}

# 等系统真的起来。service.d 的执行时机比 boot_completed 早，
# 这时候 am 命令可能还不可用，得等。
wait_boot() {
    i=0
    while [ $i -lt 120 ]; do
        b=$(getprop sys.boot_completed 2>/dev/null | tr -d '\r')
        [ "$b" = "1" ] && return 0
        sleep 2
        i=$((i + 1))
    done
    return 1
}

log "启动 pid=$$（间隔 ${INTERVAL}s）"
if ! wait_boot; then
    log "等 boot_completed 超时（240s），仍然继续跑"
fi
log "系统就绪，开始看守 $PKG"

fails=0
last_note=""

while true; do
    pid=$(pidof "$PKG" 2>/dev/null | tr -d '\r' | awk '{print $1}')

    if [ -n "$pid" ]; then
        if [ "$last_note" != "alive" ]; then
            log "Tasker 在（pid=$pid）"
            last_note="alive"
        fi
        fails=0
    else
        # 拉起。注意 am start 自己也会失败（比如系统还在初始化），
        # 失败就下轮重试，不增加间隔——Tasker 挂了就是要尽快回来。
        out=$(am start -n "$ACT" 2>&1 | tr -d '\r' | head -2 | tr '\n' ' ')
        fails=$((fails + 1))
        log "Tasker 不在，第 $fails 次拉起：$out"
        last_note="dead"
        sleep 5
        pid2=$(pidof "$PKG" 2>/dev/null | tr -d '\r' | awk '{print $1}')
        if [ -n "$pid2" ]; then
            log "已拉起（pid=$pid2）"
            last_note="alive"
            fails=0
        else
            log "拉起后仍未见进程，稍后重试"
        fi
    fi

    # 每 20 轮（约 5 分钟）整理一次日志
    trim_log
    nap
done
