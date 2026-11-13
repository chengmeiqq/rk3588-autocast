#!/usr/bin/env python3
"""逐个测全镜直播的镜头：能否开流 + 能否出 4K 帧。

用法: probe_lens.py [host:port]
不改任何持久配置，只做开关流测试，结束后恢复现场。
"""
import json
import subprocess
import sys
import time
import urllib.request

BASE = f"http://{sys.argv[1] if len(sys.argv) > 1 else '192.168.1.119:41737'}"
ADB = "/usr/lib/android-sdk/platform-tools/adb"
SERIAL = "892e20c9"
PKG = "com.opencode.multilensipcam"


def req(path, timeout=25):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            body = r.read()
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode()


def state():
    code, body = req("/api/state")
    if code != 200:
        return None
    return json.loads(body)


def adb(*args):
    return subprocess.run([ADB, "-s", SERIAL, *args], capture_output=True, text=True)


def app_alive():
    return bool(adb("shell", "pidof " + PKG).stdout.strip())


def restart_app():
    adb("shell", "am force-stop " + PKG)
    time.sleep(2)
    adb("shell", f"am start -n {PKG}/.MainActivity")
    for _ in range(20):
        time.sleep(1)
        code, _ = req("/api/state", timeout=3)
        if code == 200:
            return True
    return False


def grab_frame():
    """取一帧，返回 (res, bytes) 或 (None, 原因)。走 302 跳转。"""
    code, body = req("/snapshot.jpg", timeout=30)
    if code == 302:
        # http.client 不自动跟随，手动再来一次拿到 Location 的目标
        pass
    try:
        out = subprocess.run(
            ["curl", "-sL", "-m", "30", "-o", "/tmp/_probe.jpg",
             "-w", "%{http_code} %{size_download}", BASE + "/snapshot.jpg"],
            capture_output=True, text=True, timeout=40).stdout.split()
        if len(out) == 2 and out[0] == "200" and int(out[1]) > 10000:
            try:
                from PIL import Image
                with Image.open("/tmp/_probe.jpg") as im:
                    return f"{im.size[0]}x{im.size[1]}", int(out[1])
            except Exception as e:
                return None, f"JPEG 解析失败 {e}"
        return None, f"HTTP={out[0] if out else '?'} size={out[1] if len(out) > 1 else 0}"
    except Exception as e:
        return None, str(e)


def crash_reason():
    r = adb("shell", "su -c 'logcat -d -t 150 2>/dev/null | grep -i am_crash | "
                     "grep multilens | tail -1'")
    line = r.stdout.strip()
    if not line:
        return ""
    if "disabled by policy" in line:
        return "Camera disabled by policy"
    if "FATAL" in line or "am_crash" in line:
        return line.split("am_crash:")[-1][:100]
    return line[:100]


def test_camera(key, label):
    print(f"\n{'='*60}\n测试 {label} (camera={key})\n{'='*60}")
    if not app_alive():
        print("  App 未运行，先启动")
        if not restart_app():
            print("  ✗ 启动失败")
            return None

    # 停掉上一轮
    req("/api/control?streaming=true&stopStreaming=true", timeout=10)
    time.sleep(2)
    if not app_alive():
        restart_app()

    code, body = req(f"/api/control?camera={key}")
    print(f"  设镜头: HTTP={code} {body[:60].decode(errors='replace')}")
    time.sleep(2)

    if not app_alive():
        print(f"  ✗ 设镜头就崩了: {crash_reason()}")
        restart_app()
        return "crash-on-select"

    req("/api/control?resolution=3840x2160")
    req("/api/control?quality=100")
    time.sleep(1)

    code, body = req("/api/control?streaming=true")
    print(f"  开流: HTTP={code} {body[:60].decode(errors='replace')}")
    time.sleep(8)

    if not app_alive():
        print(f"  ✗ 开流后崩溃: {crash_reason()}")
        restart_app()
        return "crash-on-start"

    st = state()
    if st:
        print(f"  streaming={st.get('streaming')} cam={st.get('selectedCameraLabel')}")

    dim, info = grab_frame()
    if dim:
        print(f"  ✓ 出图 {dim} ({info} bytes)")
        return f"OK {dim}"
    print(f"  ✗ 无帧: {info}")
    return f"no-frame ({info})"


def main():
    print(f"目标: {BASE}\n包: {PKG}")
    st = state()
    if not st:
        print("App 不可达，启动中...")
        restart_app()
        st = state()
    if not st:
        print("✗ 无法连接 App，退出")
        return

    opts = st.get("cameraOptions", [])
    print(f"\n枚举到 {len(opts)} 个镜头:")
    for o in opts:
        print(f"  {o['label']:8} → camera={o['value']}")

    results = {}
    for o in opts:
        results[o["label"]] = test_camera(o["value"], o["label"])

    print(f"\n{'='*60}\n汇总\n{'='*60}")
    for k, v in results.items():
        print(f"  {k:8} : {v}")

    # 收尾：停流，不留后台耗电
    req("/api/control?streaming=true&stopStreaming=true", timeout=10)
    print("\n已停止直播")


if __name__ == "__main__":
    main()
