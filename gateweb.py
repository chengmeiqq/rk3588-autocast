#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守护总控台：一个网页开关所有自动守护。

为什么需要它（用户原话）：
  "我调试的时候他老是自动弹出来" —— 四个守护各自跑，调试时被抢屏幕、
  被顶前台、被重建投屏，只能 systemctl stop 一个个人肉关，关完还要记得开回来，
  很容易漏掉一个，第二天发现自动投屏整晚没工作。

设计取舍：
  1. 开关 = 暂停标记，不是停服务。
     服务进程始终活着，只是每轮开头看一眼 gate。好处：按一下 1 秒内生效，
     不需要等 systemd RestartSec；恢复也是立刻，不用等服务重启。
  2. 状态是"读文件 + systemctl is-active"，不缓存。
     你在网页上看到的就是当下真实状态，刷新一下一定准。
  3. 只做开关和状态，不做参数设置。
     参数已经在 LensCast 自己的 41737 页面里了，重复造一份只会两边不同步。

端口 8140，避免和 airweb(8123)、smartcast(8130) 打架。
"""
from __future__ import print_function

import json
import os
import subprocess
import sys
import time

from flask import Flask, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gate  # noqa: E402
import arb  # 前台仲裁状态
import detail  # 细节点采集（豆包/直播两条链路）

app = Flask(__name__)

# 守护 -> 人话名字 + 它到底会"弹"什么
DAEMONS = [
    ("adbd", "adb 连接守护", "USB 掉了自动改 WiFi、拉起 adb server"),
    ("smartcast", "投屏接口", "接受 /cast 请求，执行点击投屏"),
    ("keepcast", "投屏守护", "发现没投屏就自动点回去"),
    ("lenscast", "直播守护", "顶前台、唤醒屏幕、改直播参数"),
]

PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>守护总控台</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --line:#262b36; --txt:#e6e9ef;
          --dim:#8b93a5; --ok:#2ecc71; --warn:#f5a623; --bad:#e74c3c; }
  * { box-sizing:border-box; }
  body { margin:0; padding:20px 16px 40px; background:var(--bg); color:var(--txt);
         font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:720px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; font-weight:600; }
  .sub { color:var(--dim); font-size:13px; margin-bottom:20px; }
  .master { background:var(--card); border:1px solid var(--line); border-radius:12px;
            padding:18px; margin-bottom:18px; display:flex; align-items:center;
            justify-content:space-between; gap:16px; flex-wrap:wrap; }
  .master .label { font-size:17px; font-weight:600; }
  .master .hint { color:var(--dim); font-size:13px; margin-top:2px; }
  .list { display:flex; flex-direction:column; gap:10px; }
  .row { background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:14px 16px; display:flex; align-items:center; gap:14px; }
  .row.off { opacity:.55; }
  .row .meta { flex:1; min-width:0; }
  .row .name { font-weight:600; }
  .row .desc { color:var(--dim); font-size:12.5px; }
  .dot { width:8px; height:8px; border-radius:50%; flex:none; }
  .dot.on { background:var(--ok); box-shadow:0 0 8px var(--ok); }
  .dot.off { background:var(--bad); }
  .dot.na { background:var(--dim); }
  .sw { position:relative; width:52px; height:30px; flex:none; }
  .sw input { opacity:0; width:0; height:0; }
  .sw span { position:absolute; inset:0; background:#3a4050; border-radius:999px;
             cursor:pointer; transition:.18s; }
  .sw span:before { content:""; position:absolute; width:24px; height:24px; left:3px;
                    top:3px; background:#fff; border-radius:50%; transition:.18s; }
  .sw input:checked + span { background:var(--ok); }
  .sw input:checked + span:before { transform:translateX(22px); }
  .sw input:disabled + span { opacity:.4; cursor:not-allowed; }
  .svc { font-size:11.5px; color:var(--dim); font-family:ui-monospace,monospace; }
  .bar { margin-top:22px; display:flex; gap:8px; flex-wrap:wrap; }
  button { background:#252a35; color:var(--txt); border:1px solid var(--line);
           border-radius:8px; padding:9px 14px; font-size:13.5px; cursor:pointer; }
  button:hover { background:#2f3644; }
  #note { color:var(--warn); font-size:13px; margin-top:14px; min-height:20px; }
  .foot { color:var(--dim); font-size:12px; margin-top:26px; line-height:1.9; }
  .foot code { background:#181b22; padding:1px 5px; border-radius:4px; }
</style></head>
<body><div class="wrap">
<h1>守护总控台</h1>
<div class="sub">调试时按「全部暂停」，守护立刻停手，连一条 adb 命令都不发。</div>

<div class="master">
  <div>
    <div class="label" id="mLabel">全部守护</div>
    <div class="hint" id="mHint">载入中…</div>
  </div>
  <label class="sw"><input type="checkbox" id="mSw"><span></span></label>
</div>

<div class="list" id="list"></div>

<div id="pw" class="pw" style="display:none"></div>

<div class="chain">
  <div class="ch">
    <div class="chh"><span class="cht">豆包 · 投屏链路</span><span class="chs" id="dbSum"></span></div>
    <div class="grid" id="dbGrid"></div>
  </div>
  <div class="ch">
    <div class="chh"><span class="cht">直播 · 推流链路</span><span class="chs" id="lcSum"></span></div>
    <div class="grid" id="lcGrid"></div>
  </div>
</div>

<div class="bar">
  <button onclick="act('pause')">全部暂停</button>
  <button onclick="act('resume')">全部恢复</button>
  <button onclick="load()">刷新</button>
</div>
<div id="note"></div>
<div id="arb" class="arb"></div>

<style>
.arb{font-size:12px;color:#7a8595;margin:10px 0 4px;line-height:1.6}
.pw{background:#3a2418;border:1px solid #6b4520;color:#ffb26b;
    border-radius:10px;padding:10px 13px;font-size:13px;margin:16px 0 0;
    line-height:1.55}
.chain{margin:22px 0 6px}
.ch{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:14px 16px;margin-bottom:12px}
.chh{display:flex;justify-content:space-between;align-items:baseline;
     margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.cht{font-size:14px;font-weight:600;letter-spacing:.3px}
.chs{font-size:12px;color:var(--dim)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px}
@media(max-width:520px){.grid{grid-template-columns:1fr}}
.item{display:flex;align-items:center;gap:7px;font-size:13px;
      padding:3px 0;min-width:0}
.item .bd{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
.bd.on{background:var(--ok);box-shadow:0 0 6px rgba(46,204,113,.6)}
.bd.off{background:var(--bad);box-shadow:0 0 6px rgba(231,76,60,.5)}
.bd.na{background:#4a5263}
.item .k{color:var(--dim);flex:0 0 auto}
.item .v{color:var(--txt);overflow:hidden;text-overflow:ellipsis;
         white-space:nowrap;font-variant-numeric:tabular-nums}
.item .v.bad{color:var(--bad)}
.item .v.good{color:var(--ok)}
</style>
<div class="foot">
  暂停是「就地让路」：服务进程不退出，只是每轮跳过动作，按一下 1 秒内生效。<br>
  恢复后不需要重启服务。参数调节在 <code>手机IP:41737</code>。
</div>
</div>
<script>
const DAEMONS = __DAEMONS__;
let busy = false;

function el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt !== undefined) e.textContent = txt;
  return e;
}

function render(s) {
  const allOn = s.all;
  document.getElementById('mSw').checked = allOn;
  document.getElementById('mLabel').textContent = allOn ? '全部守护：运行中' : '全部守护：已暂停';
  document.getElementById('mHint').textContent = allOn
    ? '四个守护都在按规则自动接管'
    : (s.note ? '已暂停 · ' + s.note : '已暂停，守护不会动手');
  document.getElementById('mSw').disabled = busy;

  // 前台仲裁：两个守护轮流用前台，这里显示谁刚动过手。
  const arbBox = document.getElementById('arb');
  if (arbBox && s.arb) {
    const g = s.arb.gap_seconds;
    const parts = [];
    for (const [k, v] of Object.entries(s.arb.daemons || {})) {
      if (v.since === null || v.since === undefined) {
        parts.push(k + '：还没动过');
      } else {
        parts.push(k + '：' + v.since.toFixed(0) + ' 秒前' + (v.what ? '（' + v.what + '）' : ''));
      }
    }
    arbBox.textContent = '前台间隔 ' + g + ' 秒 · ' + parts.join(' · ');
  }

  const list = document.getElementById('list');
  list.innerHTML = '';
  for (const [id, name, desc] of DAEMONS) {
    const d = s.daemons[id] || {};
    const allowed = !!d.allowed;
    const row = el('div', 'row' + (allowed ? '' : ' off'));
    const dot = el('div', 'dot ' + (allowed ? 'on' : 'off'));
    const meta = el('div', 'meta');
    meta.appendChild(el('div', 'name', name));
    meta.appendChild(el('div', 'desc', desc));
    meta.appendChild(el('div', 'svc', 'service: ' + (d.service || '?')));
    const sw = el('label', 'sw');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!d.enabled && allOn;
    cb.disabled = busy || !allOn;
    cb.onchange = () => toggle(id, cb.checked);
    sw.appendChild(cb);
    sw.appendChild(el('span'));
    row.appendChild(dot); row.appendChild(meta); row.appendChild(sw);
    list.appendChild(row);
  }

  renderChain('db', s.doubao || [], 'dbGrid', 'dbSum');
  renderChain('lc', s.lens || [], 'lcGrid', 'lcSum');

  // 手机卡住/探测熔断时，红点的一堆"没在跑"其实都是同一个原因，
  // 不说明的话会误以为 App 崩了。
  const warn = document.getElementById('pw');
  if (warn) {
    if (s.probe_down) {
      warn.textContent = '⚠ ' + (s.probe_note || 'adb 探测超时，手机可能卡住了；点位不代表真实状态');
      warn.style.display = 'block';
    } else {
      warn.style.display = 'none';
    }
  }
}

// 一条链路的所有细节点。灰点=信息项，红/绿=能判定成败的。
function renderChain(prefix, rows, gridId, sumId) {
  const g = document.getElementById(gridId);
  if (!g) return;
  g.innerHTML = '';
  let bad = 0, tot = 0;
  for (const r of rows) {
    const it = el('div', 'item');
    const d = el('div', 'bd ' + (r.ok === null ? 'na' : (r.ok ? 'on' : 'off')));
    it.appendChild(d);
    it.appendChild(el('span', 'k', r.label));
    const v = el('span', 'v' + (r.ok === null ? '' : (r.ok ? ' good' : ' bad')), r.value);
    v.title = r.label + '：' + r.value;
    it.appendChild(v);
    g.appendChild(it);
    if (r.ok !== null) { tot++; if (!r.ok) bad++; }
  }
  const sum = document.getElementById(sumId);
  if (sum) {
    sum.textContent = bad === 0 ? (tot + ' 项全部正常') : (bad + '/' + tot + ' 项异常');
    sum.style.color = bad === 0 ? 'var(--ok)' : 'var(--bad)';
  }
}

async function load() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    render(s);
  } catch (e) {
    document.getElementById('note').textContent = '读取失败：' + e;
  }
}

async function act(what, who) {
  if (busy) return;
  busy = true;
  document.getElementById('note').textContent = '处理中…';
  try {
    const r = await fetch('/api/' + what, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(who ? {daemon: who} : {})
    });
    const j = await r.json();
    document.getElementById('note').textContent = j.msg || '';
  } catch (e) {
    document.getElementById('note').textContent = '操作失败：' + e;
  }
  busy = false;
  await load();
}

function toggle(id, on) { act(on ? 'resume' : 'pause', id); }

document.getElementById('mSw').onchange = (e) =>
  act(e.target.checked ? 'resume' : 'pause');

load();
setInterval(load, 4000);
</script>
</body></html>
"""


def svc_state(name):
    """读取 systemd 状态；不是 systemd 起的就报 -。"""
    try:
        a = subprocess.run(["systemctl", "is-active", "airtest-" + name],
                           capture_output=True, text=True, timeout=5)
        return (a.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def serial_for_detail():
    """细节点要一个设备序列号。拿不到就返回空，detail 会自己降级。"""
    try:
        import smartcast
        return smartcast.pick_serial()
    except Exception:
        return ""


def build_status():
    s = gate.status()
    for ident, _, _ in DAEMONS:
        st = svc_state(ident)
        s["daemons"][ident]["service"] = st
        # 服务没在跑的话，开关显示成灰的更有意义
        s["daemons"][ident]["running"] = st == "active"
    s["at_now"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # 前台仲裁：谁刚动过前台、隔了多久。两个守护就是靠这个错开的，
    # 放在页面上才看得出"它们到底在轮什么"。
    try:
        s["arb"] = arb.status()
    except Exception as e:
        s["arb"] = {"error": str(e)[:80]}

    # 细节点：豆包投屏链路 + 直播推流链路。
    # 采集会跑 adb，失败绝不能把整个 /api/status 拖挂，否则页面全白。
    try:
        det = detail.collect(serial_for_detail())
        s["doubao"] = det.get("doubao", [])
        s["lens"] = det.get("lens", [])
        s["probe_at"] = det.get("at", "")
        s["probe_age"] = det.get("age", 0)
        s["probe_down"] = bool(det.get("probe_down"))
        s["probe_note"] = det.get("probe_note", "")
    except Exception as e:
        s["doubao"] = []
        s["lens"] = []
        s["probe_error"] = str(e)[:120]
    return s


@app.route("/")
def index():
    items = json.dumps([[a, b, c] for a, b, c in DAEMONS], ensure_ascii=False)
    return PAGE.replace("__DAEMONS__", items)


@app.route("/api/status")
def api_status():
    return jsonify(build_status())


@app.route("/api/pause", methods=["POST"])
def api_pause():
    who = (request.get_json(silent=True) or {}).get("daemon")
    gate.pause(who, note="网页手动暂停")
    label = "全部" if not who else who
    return jsonify({"ok": True, "msg": "%s 已暂停，守护立刻停手" % label})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    who = (request.get_json(silent=True) or {}).get("daemon")
    gate.resume(who, note="")
    label = "全部" if not who else who
    return jsonify({"ok": True, "msg": "%s 已恢复" % label})


@app.route("/health")
def health():
    s = build_status()
    return jsonify({"ok": True, "allowed": s["all"]})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8140
    print("守护总控台：http://0.0.0.0:%d/" % port, flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
