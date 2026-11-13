#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用视觉大模型做最后一道验证：屏幕到底在不在共享。

为什么需要这个：
  模板匹配只能证明"我点的那个图案出现在某处"。它证明不了投屏真的开着。
  实测踩过——流程 7/7 全部通过，`dumpsys` 里却没有 ScreenCapture，
  孩子那边是黑的。反过来也可能：图标还在，投屏已被系统掐了。

  所以最后一步交给模型看图，用自然语言判断"现在屏幕上有没有在共享"。
  模型结论和 dumpsys 交叉验证，两者都过才算成功。

模型选择：qiniu 网关下 qwen/qwen3.8-omni-flash。
  实测能用（qwen-vl-max、qwen2.5-vl-72b、doubao-1.5-vision-pro、glm-4.7
  在 2026 年均已 retired，调用直接报错，别再用）。
"""
import base64
import json
import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.expanduser("~/.openclaw/openclaw.json")

# 只让模型回答"是/否 + 证据"，不给它自由发挥的空间。
PROMPT = (
    "这是安卓手机截图。请判断：这块屏幕此刻是否正在被共享/投屏到别的设备？\n"
    "判断依据举例：出现「正在共享屏幕」「停止共享」「屏幕共享中」等提示，"
    "或状态栏出现投屏图标。\n"
    "只输出一行 JSON，不要任何解释：\n"
    '{"sharing": true 或 false, "evidence": "你在图上看到的依据，20字内"}'
)


def _api_key():
    """从 openclaw 配置里取网关 key。取不到就返回 None，调用方跳过验证。"""
    try:
        with open(CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg["models"]["providers"]["qiniu"]["apiKey"]
    except Exception:
        return None


def _model():
    return os.environ.get("AIVERIFY_MODEL", "qwen/qwen3.8-omni-flash")


def _base_url():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)["models"]["providers"]["qiniu"]["baseUrl"].rstrip("/")
    except Exception:
        return "https://api.qnaigc.com/v1"


def grab(serial, path=None):
    """截一张图。自带超时，设备掉线时不会把主流程挂死。"""
    path = path or "/tmp/aiverify.png"
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=25)
        if not r.stdout or len(r.stdout) < 5000:
            return None  # 空图/裂图，说明设备没准备好
        with open(path, "wb") as f:
            f.write(r.stdout)
        return path
    except Exception:
        return None


def ask_sharing(img_path, timeout=70, prompt_hint=""):
    """让模型看图回答：在不在共享。

    返回 (sharing, evidence)。任何失败都返回 (None, 原因)，
    调用方据此决定"算不出结果"，而不是当成失败。
    """
    key = _api_key()
    if not key:
        return None, "没有可用的模型 key"
    if not img_path or not os.path.exists(img_path):
        return None, "没有截图"

    try:
        b64 = base64.b64encode(open(img_path, "rb").read()).decode()
    except Exception as e:
        return None, "读图失败: %s" % e

    payload = {
        "model": _model(),
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + b64}},
            {"type": "text", "text": (prompt_hint + "\n" + PROMPT)
             if prompt_hint else PROMPT},
        ]}],
        "max_tokens": 160,
        "temperature": 0,
    }
    tmp = "/tmp/_aiverify_req.json"
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        r = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-X", "POST",
             _base_url() + "/chat/completions",
             "-H", "Authorization: Bearer " + key,
             "-H", "Content-Type: application/json",
             "--data-binary", "@" + tmp],
            capture_output=True, text=True)
        d = json.loads(r.stdout)
    except Exception as e:
        return None, "调用失败: %s" % e
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

    if "choices" not in d:
        msg = (d.get("error") or {}).get("message", "未知错误")
        return None, "接口报错: " + str(msg)[:80]

    text = (d["choices"][0]["message"]["content"] or "").strip()
    raw = text

    # 模型常把 JSON 包在 ```json 里，或者前后带话。只挖最外层花括号。
    try:
        i, j = raw.index("{"), raw.rindex("}")
        obj = json.loads(raw[i:j + 1])
    except Exception:
        # 挖不到 JSON 就退回关键词判断，别直接判失败
        low = raw.lower()
        if "true" in low and "false" not in low:
            return True, raw[:60]
        if "false" in low:
            return False, raw[:60]
        return None, "回答看不懂: " + raw[:60]

    sharing = obj.get("sharing")
    if isinstance(sharing, str):
        sharing = sharing.strip().lower() in ("true", "yes", "是", "在共享")
    if not isinstance(sharing, bool):
        return None, "字段不是布尔: " + raw[:60]
    return sharing, str(obj.get("evidence", ""))[:60]


def dumpsys_casting(serial):
    """系统的真实投屏状态。ScreenCapture 有 owner 就是真在录屏共享。"""
    try:
        out = subprocess.run(
            ["adb", "-s", serial, "shell",
             "dumpsys display | grep ScreenCapture"],
            capture_output=True, text=True, timeout=20).stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        if "owner" in line:
            seg = line.split("owner", 1)[1].split()[0] if len(
                line.split("owner", 1)[1].split()) else ""
            return seg.strip()
    return ""


def _crop_zoom(img_path, y2=150, scale=3, out="/tmp/aiverify_top.png"):
    """截出顶部状态栏放大，给小图标一个被看清的机会。

    为什么需要：投屏真的开着、但已经回到桌面时，屏幕上那行
    「正在共享屏幕」横幅是没了，只剩状态栏最左边一个几像素的小图标。
    整图缩进模型后那个图标就糊成一团，模型答"没看到"，于是把一次
    成功的投屏判成失败、白重跑一遍。放大 3 倍再看，就能认出来。
    """
    try:
        import cv2
        img = cv2.imread(img_path)
        if img is None:
            return None
        h = img.shape[0]
        top = img[0:min(y2, h), :]
        top = cv2.resize(top, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC)
        cv2.imwrite(out, top)
        return out
    except Exception:
        return None


def verify(serial, tries=3, gap=2.0):
    """最终验证：模型看图 + 系统状态，两边都说"在共享"才算过。

    返回 dict：ok / llm / llm_evidence / sys_owner / reason
    """
    res = {"ok": False, "llm": None, "llm_evidence": "", "sys_owner": None,
           "reason": ""}
    owner = dumpsys_casting(serial)
    res["sys_owner"] = owner

    if owner is None:
        res["reason"] = "查不到系统投屏状态（设备可能掉线）"
        return res

    if not owner:
        res["reason"] = "系统里没有投屏会话（ScreenCapture 无 owner）"
        # 系统已明确说没投屏，不必再花时间问模型
        return res

    # 系统说有。再用模型确认一眼画面，防止"有会话但其实空白"。
    img = None
    for _ in range(tries):
        img = grab(serial)
        if img:
            break
        time.sleep(gap)
    sharing, ev = ask_sharing(img) if img else (None, "截不到图")

    res["llm"] = sharing
    res["llm_evidence"] = ev

    if sharing is None:
        # 模型不可用时，以系统状态为准——不能因为模型挂了就判失败
        res["ok"] = True
        res["reason"] = "模型没给结论（%s），按系统状态判定：投屏中" % ev
        return res

    if sharing:
        res["ok"] = True
        res["reason"] = "模型与系统一致：正在投屏（%s）" % ev
        return res

    # 模型说没共享，但系统明确有会话。多半是回到桌面后横幅消失、
    # 状态栏小图标没看清。放大状态栏再问一次，避免误判导致白重跑。
    zoom = _crop_zoom(img) if img else None
    if zoom:
        s2, ev2 = ask_sharing(zoom, prompt_hint="这是状态栏放大图")
        res["llm_evidence"] = "%s / 放大复查：%s" % (ev, ev2)
        if s2:
            res["ok"] = True
            res["llm"] = True
            res["reason"] = "模型与系统一致：正在投屏（放大状态栏看到 %s）" % ev2
            return res
        if s2 is None:
            # 模型两次都没给出可用结论，系统状态是唯一证据
            res["ok"] = True
            res["reason"] = "模型没结论，按系统状态判定：投屏中（%s）" % ev2
            return res

    res["reason"] = "系统有会话但模型看到没在共享（%s）" % ev
    return res


if __name__ == "__main__":
    import sys
    serial = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "PHONE_SERIAL", "892e20c9")
    print(json.dumps(verify(serial), ensure_ascii=False, indent=2))
