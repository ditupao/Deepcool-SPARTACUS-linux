#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The SPARTACUS-linux Authors
# SPDX-License-Identifier: MIT
#
# Provided for research, education and interoperability purposes only.
# AS IS, no warranty; see LICENSE/NOTICE. Web console has no auth: LAN only.
#
# spartacus_web - web console for the Spartacus AIO LCD driver:
# dashboard layout toggles, media library (image / GIF / video upload,
# non-destructive crop framing) and OSD overlay configuration.
# See NOTICE for referenced community projects.
#
# 冰暴屏幕网页控制台 (:8085)
# 特性: 上传 image/gif/video, 非破坏式方形取景(平移+缩放), 帧缓存重建
import json, os, time, shutil, subprocess
import glob as g
from flask import Flask, request, jsonify, send_file, Response
from PIL import Image, ImageOps

SETTINGS_PATH = "/etc/spartacusd/settings.json"
MEDIA_ROOT = "/var/lib/spartacus/media"
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
GIF_EXT = {".gif"}
VID_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}

def load_settings():
    try:
        with open(SETTINGS_PATH) as f: s = json.load(f)
    except Exception: s = {}
    s.setdefault("mode", "dashboard")
    s.setdefault("media", None)
    s.setdefault("dashboard", {"title": "SPARTACUS · Linux", "cpu": True, "gpu": True,
        "board": True, "pump": True, "fan": True, "cpu_use": False, "load": True, "clock": True})
    s.setdefault("overlay", {"enabled": False, "pos": "br", "size": "M",
        "items": {"cpu": True, "gpu": False, "board": False, "power_cpu": True,
                  "power_gpu": False, "cpu_use": False, "load": False, "pump": False,
                  "fan": False, "clock": False}})
    s.setdefault("screensaver", {"style": "bounce", "clock": True})
    return s

_rapl = {"e": None, "t": None, "max": None}
def cpu_power():
    try:
        e = int(open("/sys/class/powercap/intel-rapl:0/energy_uj").read())
        now = time.monotonic()
    except Exception:
        return None
    if _rapl["max"] is None:
        try: _rapl["max"] = int(open("/sys/class/powercap/intel-rapl:0/max_energy_range_uj").read())
        except Exception: _rapl["max"] = 2 ** 63
    p, t = _rapl["e"], _rapl["t"]
    _rapl.update(e=e, t=now)
    if p is None: return None
    de = e - p
    if de < 0: de += _rapl["max"]
    dt = now - t
    if dt <= 0: return None
    return de / 1e6 / dt

def save_settings(s):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1, ensure_ascii=False)
    os.rename(tmp, SETTINGS_PATH)

def read_sensors():
    hw = {}
    for p in g.glob("/sys/class/hwmon/hwmon*"):
        try: hw[open(p + "/name").read().strip()] = p
        except Exception: pass
    d = {}
    try: d["cpu"] = round(int(open(hw["coretemp"] + "/temp1_input").read()) / 1000, 1)
    except Exception: pass
    try: d["gpu"] = round(int(open(hw["amdgpu"] + "/temp1_input").read()) / 1000, 1)
    except Exception: pass
    try: d["board"] = round(int(open(hw["nct6798"] + "/temp6_input").read()) / 1000, 1)
    except Exception: pass
    try:
        b = hw.get("nct6798")
        d["pump"] = int(open(b + "/fan2_input").read())
        d["fan"] = int(open(b + "/fan1_input").read())
    except Exception: pass
    try: d["load"] = float(open("/proc/loadavg").read().split()[0])
    except Exception: pass
    try: d["power_gpu"] = round(int(open(hw["amdgpu"] + "/power1_average").read()) / 1e6, 1)
    except Exception: pass
    w = cpu_power()
    if w is not None: d["power_cpu"] = round(w, 1)
    return d

def cover(im, size=480):
    w, h = im.size
    sc = max(size / w, size / h)
    im = im.resize((max(size, int(w * sc)), max(size, int(h * sc))), Image.LANCZOS)
    w, h = im.size
    x, y = (w - size) // 2, (h - size) // 2
    return im.crop((x, y, x + size, y + size))

def sq_crop(im, c):
    """c = {x,y,w} 均为相对原图宽度的比例(0..1)，取方形区域"""
    W, H = im.size
    side = max(16, min(int(c["w"] * W), W, H))
    x = min(max(0, int(c["x"] * W)), W - side)
    y = min(max(0, int(c["y"] * H)), H - side)
    return im.crop((x, y, x + side, y + side))

def save_jpg(im, path, q=85):
    t = path + ".tmp"
    im.save(t, "JPEG", quality=q)
    os.replace(t, path)

def find_src(d):
    m = g.glob(os.path.join(d, "_src*"))
    return m[0] if m else None

def vprobe(src):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", src],
                         capture_output=True, check=True).stdout.decode().strip()
    w, h = (int(x) for x in out.split("x")[:2])
    return w, h

def dur_probe(src):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", src], capture_output=True, check=True).stdout
        return float(out.strip())
    except Exception:
        return 30.0

def render_image(d, crop):
    im = ImageOps.exif_transpose(Image.open(find_src(d))).convert("RGB")
    if crop: im = sq_crop(im, crop)
    save_jpg(cover(im), os.path.join(d, "f00001.jpg"))

def render_gif(d, crop):
    im = Image.open(find_src(d))
    i, durs = 0, []
    while i < 800:
        try: im.seek(i)
        except EOFError: break
        fr = im.convert("RGBA")
        base = Image.new("RGB", fr.size, (0, 0, 0))
        base.paste(fr, (0, 0), fr)
        if crop: base = sq_crop(base, crop)
        save_jpg(cover(base), os.path.join(d, "f%05d.jpg" % (i + 1)), q=80)
        durs.append(max(40, int(im.info.get("duration") or 100)))
        i += 1
    if not i: raise ValueError("GIF 无有效帧")
    avg = sum(durs) / len(durs)
    return {"frames": i, "fps": round(1000.0 / avg, 2), "durations": durs,
            "duration": round(sum(durs) / 1000.0, 1)}

def render_video(d, crop, fps):
    src = find_src(d)
    if crop:
        W, H = vprobe(src)
        side = max(16, min(int(crop["w"] * W), W, H)); side -= side % 2
        x = min(max(0, int(crop["x"] * W)), W - side); x -= x % 2
        y = min(max(0, int(crop["y"] * H)), H - side); y -= y % 2
        vf = "fps=%d,crop=%d:%d:%d:%d,scale=480:480:flags=lanczos" % (fps, side, side, x, y)
    else:
        vf = ("fps=%d,scale=480:480:force_original_aspect_ratio=decrease:flags=lanczos,"
              "pad=480:480:(ow-iw)/2:(oh-ih)/2:black" % fps)
    p = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", src,
                        "-vf", vf, "-q:v", "4", os.path.join(d, "f%05d.jpg")],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or b"").decode("utf-8", "replace")[-400:])

def rebuild(d, m, crop):
    """从 _src 重建帧缓存(crop=None 为原始 cover 模式)"""
    k = m["kind"]
    if k == "image":
        render_image(d, crop)
    elif k == "gif":
        info = render_gif(d, crop)
        m.update(info)
    else:
        render_video(d, crop, m.get("fps") or 12)
        m["frames"] = len(g.glob(os.path.join(d, "f*.jpg")))
    m["crop"] = crop
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump(m, f, ensure_ascii=False)

def load_manifest(d):
    with open(os.path.join(d, "manifest.json")) as f:
        return json.load(f)

@app.get("/")
def index():
    return Response(HTML, "text/html; charset=utf-8")

@app.get("/api/state")
def api_state():
    s = load_settings()
    media = None
    if s.get("media"):
        try: media = load_manifest(os.path.join(MEDIA_ROOT, os.path.basename(str(s["media"]))))
        except Exception: media = {"id": s["media"], "title": "(丢失)"}
    return jsonify(mode=s["mode"], media=media, sensors=read_sensors(),
                   dashboard=s["dashboard"], overlay=s["overlay"],
                   screensaver=s["screensaver"])

@app.get("/api/media/list")
def api_list():
    out = []
    for mf in g.glob(os.path.join(MEDIA_ROOT, "*", "manifest.json")):
        try: out.append(load_manifest(os.path.dirname(mf)))
        except Exception: pass
    out.sort(key=lambda m: -m.get("created", 0))
    return jsonify(out)

@app.post("/api/dashboard")
def api_dashboard():
    data = request.get_json(force=True)
    s = load_settings()
    s["dashboard"] = data.get("dashboard", data)
    s["mode"] = "dashboard"
    save_settings(s)
    return jsonify(ok=True)

@app.post("/api/back")
def api_back():
    s = load_settings(); s["mode"] = "dashboard"
    save_settings(s)
    return jsonify(ok=True)

@app.post("/api/screensaver")
def api_screensaver():
    data = request.get_json(force=True)
    st = data.get("style", "bounce")
    if st not in ("bounce", "gradient", "stars"):
        return jsonify(error="样式参数无效"), 400
    s = load_settings()
    s["screensaver"] = {"style": st, "clock": bool(data.get("clock", True))}
    s["mode"] = "screensaver"
    save_settings(s)
    return jsonify(ok=True, screensaver=s["screensaver"])

@app.post("/api/overlay")
def api_overlay():
    data = request.get_json(force=True)
    o = data.get("overlay", data)
    if str(o.get("pos", "br"))[0] not in "tb" or str(o.get("pos", "br"))[-1] not in "lcr":
        return jsonify(error="位置参数无效"), 400
    if o.get("size") not in ("S", "M", "L"):
        return jsonify(error="字号参数无效"), 400
    s = load_settings()
    cur = s.setdefault("overlay", {"items": {}})
    cur["enabled"] = bool(o.get("enabled"))
    cur["pos"] = str(o.get("pos", "br"))
    cur["size"] = str(o.get("size", "M"))
    cur["items"] = {k: bool(v) for k, v in (o.get("items") or {}).items()}
    save_settings(s)
    return jsonify(ok=True, overlay=cur)

@app.post("/api/media/upload")
def api_upload():
    f = request.files.get("file")
    if not f: return jsonify(error="没有文件"), 400
    name = f.filename or "media"
    ext = os.path.splitext(name)[1].lower()
    if ext not in (IMG_EXT | GIF_EXT | VID_EXT):
        return jsonify(error="格式不支持: " + ext), 400
    kind = "image" if ext in IMG_EXT else ("gif" if ext in GIF_EXT else "video")
    mid = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    d = os.path.join(MEDIA_ROOT, mid)
    os.makedirs(d, exist_ok=True)
    srcf = os.path.join(d, "_src" + ext)
    f.save(srcf)
    m = {"id": mid, "title": name, "kind": kind, "crop": None, "created": int(time.time())}
    try:
        if kind == "image":
            render_image(d, None); m.update(frames=1, fps=2)
        elif kind == "gif":
            m.update(render_gif(d, None))
        else:
            dur = dur_probe(srcf)
            fps = max(4, min(12, int(3500 // max(dur, 1))))
            render_video(d, None, fps)
            m.update(fps=fps, duration=round(dur, 1),
                     frames=len(g.glob(os.path.join(d, "f*.jpg"))))
        if not m.get("frames"): raise RuntimeError("转码无输出帧")
        with open(os.path.join(d, "manifest.json"), "w") as fp:
            json.dump(m, fp, ensure_ascii=False)
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error=str(e)[:400]), 500
    return jsonify(m)

@app.post("/api/media/crop")
def api_crop():
    data = request.get_json(force=True)
    mid = os.path.basename(str(data.get("id", "")))
    d = os.path.join(MEDIA_ROOT, mid)
    try: m = load_manifest(d)
    except Exception: return jsonify(error="媒体不存在"), 404
    if not find_src(d): return jsonify(error="原始文件丢失，无法取景"), 409
    try:
        crop = {"x": float(data["x"]), "y": float(data["y"]), "w": float(data["w"])}
        for k, v in crop.items():
            if not (0.0 <= v <= 1.0): raise ValueError("取景参数越界: " + k)
        rebuild(d, m, crop)
    except subprocess.CalledProcessError as e:
        return jsonify(error="转码失败: " + (e.stderr or b"").decode("utf-8", "replace")[-300:]), 500
    except Exception as e:
        return jsonify(error=str(e)[:400]), 500
    return jsonify(m)

@app.post("/api/media/reset")
def api_reset():
    data = request.get_json(force=True)
    mid = os.path.basename(str(data.get("id", "")))
    d = os.path.join(MEDIA_ROOT, mid)
    try: m = load_manifest(d)
    except Exception: return jsonify(error="媒体不存在"), 404
    if not find_src(d): return jsonify(error="原始文件丢失"), 409
    try:
        rebuild(d, m, None)
    except Exception as e:
        return jsonify(error=str(e)[:400]), 500
    return jsonify(m)

@app.post("/api/media/activate")
def api_activate():
    data = request.get_json(force=True)
    mid = os.path.basename(str(data.get("id", "")))
    if not os.path.exists(os.path.join(MEDIA_ROOT, mid, "manifest.json")):
        return jsonify(error="媒体不存在"), 404
    s = load_settings(); s["media"] = mid; s["mode"] = "media"
    save_settings(s)
    return jsonify(ok=True)

@app.post("/api/media/delete")
def api_delete():
    data = request.get_json(force=True)
    mid = os.path.basename(str(data.get("id", "")))
    d = os.path.join(MEDIA_ROOT, mid)
    if not os.path.isdir(d): return jsonify(error="媒体不存在"), 404
    shutil.rmtree(d, ignore_errors=True)
    s = load_settings()
    if s.get("media") == mid:
        s["media"] = None; s["mode"] = "dashboard"
        save_settings(s)
    return jsonify(ok=True)

@app.get("/media/<mid>/thumb")
def api_thumb(mid):
    p = os.path.join(MEDIA_ROOT, os.path.basename(str(mid)), "f00001.jpg")
    if not os.path.exists(p): return "none", 404
    return send_file(p, mimetype="image/jpeg")

@app.get("/media/<mid>/base")
def api_base(mid):
    """编辑器底图 = 未裁剪的原始源(image/gif)或视频首帧 poster"""
    d = os.path.join(MEDIA_ROOT, os.path.basename(str(mid)))
    src = find_src(d)
    if not src: return "none", 404
    try: m = load_manifest(d)
    except Exception: return "none", 404
    if m["kind"] == "video":
        poster = os.path.join(d, "_poster.jpg")
        if not os.path.exists(poster):
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", src,
                            "-frames:v", "1", "-vf", "scale=850:-2", poster],
                           capture_output=True, check=True)
        return send_file(poster, mimetype="image/jpeg")
    return send_file(src)

HTML = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Spartacus 屏幕控制台</title>
<style>
:root{--bg:#12141f;--card:#1c1f2e;--tx:#e6e8f0;--dim:#8b8fa8;--ac:#5ac8fa}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--tx);font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
h1{font-size:21px;margin:0 0 16px}
h2{font-size:15px;margin:0 0 12px;color:var(--ac)}
.card{background:var(--card);border-radius:12px;padding:18px 20px;margin-bottom:16px;max-width:980px}
.pill{display:inline-block;font-size:12px;padding:3px 10px;border-radius:99px;background:#232738;vertical-align:middle;font-weight:400}
label{display:inline-flex;align-items:center;gap:6px;margin:4px 16px 4px 0;font-size:14px;cursor:pointer}
input[type=text]{background:#232738;border:1px solid #333a52;color:var(--tx);border-radius:6px;padding:6px 10px}
input[type=checkbox]{width:16px;height:16px;accent-color:var(--ac)}
select{background:#232738;color:var(--tx);border:1px solid #333a52;border-radius:6px;padding:6px 8px;font-size:14px}
button{background:var(--ac);border:0;color:#06121f;font-weight:600;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:14px}
button.sec{background:#2a2f45;color:var(--tx)}
button:hover{filter:brightness(1.12)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px;margin-top:14px}
.mitem{background:#232738;border-radius:10px;padding:10px;font-size:13px}
.mitem img{width:100%;height:125px;object-fit:cover;border-radius:6px;background:#000}
.mitem .t{margin:8px 0 2px;font-weight:600;word-break:break-all}
.mitem .i{color:var(--dim);font-size:12px;margin-bottom:8px}
.row{display:flex;gap:8px;flex-wrap:wrap}
.sensor{display:inline-block;background:#232738;border-radius:8px;padding:8px 14px;margin:0 8px 8px 0;font-size:12px;color:var(--dim)}
.sensor b{font-size:17px;color:var(--tx);display:block}
#upMsg,#edMsg{color:#f0b429;margin-left:10px;font-size:13px}
#edMask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:50;align-items:center;justify-content:center}
#edBox{background:var(--card);border-radius:14px;padding:18px;max-width:560px}
#edView{position:relative;width:360px;height:360px;overflow:hidden;border-radius:10px;background:#000;cursor:grab;touch-action:none;margin:0 auto}
#edView.drag{cursor:grabbing}
#edImg{position:absolute;max-width:none;user-select:none;-webkit-user-drag:none;image-rendering:auto}
#edGuide{position:absolute;inset:0;border:2px solid rgba(90,200,250,.85);border-radius:10px;pointer-events:none;box-shadow:inset 0 0 0 1px rgba(0,0,0,.4)}
#edInfo{color:var(--dim);font-size:12px;margin:10px 4px}
#edZoom{width:100%;accent-color:var(--ac);margin:4px 0 10px}
</style></head><body>
<h1>Spartacus 屏幕控制台 <span class="pill" id="modePill">连接中…</span></h1>
<div class="card"><h2>状态</h2><div id="sensors"></div></div>
<div class="card"><h2>仪表盘模式</h2>
<label>标题 <input type="text" id="title" maxlength="28" style="width:240px"></label>
<div id="switches"></div>
<div class="row" style="margin-top:12px"><button onclick="applyDash()">应用并切到仪表盘</button></div></div>
<div class="card"><h2>防烧屏屏保（独立模式 · 画面逐帧变化，像素轮换点亮）</h2>
<label>样式 <select id="savStyle">
<option value="bounce">弹跳 Logo（撞边换色）</option>
<option value="gradient">流动渐变</option>
<option value="stars">星空漫步</option>
</select></label>
<label style="font-weight:600"><input type="checkbox" id="savClock">角标时钟（位置持续微动）</label>
<div class="row" style="margin-top:12px"><button onclick="savOn()">启用屏保模式</button><span id="savMsg"></span></div></div>
<div class="card"><h2>媒体 OSD 悬浮层（播放图片/GIF/视频时叠加）</h2>
<label style="font-weight:600"><input type="checkbox" id="ovEnabled">启用悬浮层</label>
<label>位置 <select id="ovPos">
<option value="tl">左上</option><option value="tr">右上</option>
<option value="tc">顶部居中</option><option value="bl">左下</option>
<option value="br" selected>右下</option><option value="bc">底部居中</option>
</select></label>
<label>字号 <select id="ovSize"><option value="S">小</option><option value="M" selected>中</option><option value="L">大</option></select></label>
<div id="ovSwitches"></div>
<div class="row" style="margin-top:12px"><button onclick="applyOvl()">应用悬浮层</button><span id="ovMsg"></span></div></div>
<div class="card"><h2>媒体库（图片 / GIF / 视频，支持取景裁剪）</h2>
<input type="file" id="file" accept=".jpg,.jpeg,.png,.webp,.bmp,.gif,.mp4,.mkv,.webm,.mov,.avi,.m4v">
<button id="upBtn" onclick="upload()">上传并转码</button><span id="upMsg"></span>
<div class="grid" id="media"></div></div>

<div id="edMask"><div id="edBox">
<h2 style="margin-bottom:10px">取景编辑 <span class="pill" id="edKind"></span></h2>
<div id="edView"><img id="edImg" alt=""><div id="edGuide"></div></div>
<input type="range" id="edZoom" min="100" max="600" value="100" oninput="setZoom(this.value/100)">
<div id="edInfo"></div>
<div class="row">
<button onclick="edSave()">应用取景</button>
<button class="sec" onclick="edReset()">还原原始</button>
<button class="sec" onclick="edClose()">关闭</button>
<span id="edMsg"></span></div>
</div></div>

<script>
const SW=[["cpu","CPU温度(大数字)"],["gpu","GPU温度"],["power_cpu","CPU功率(PKG)"],["power_gpu","GPU功率"],["board","主板温度"],["cpu_use","CPU使用率"],["load","负载均值"],["pump","水泵转速"],["fan","风扇转速"],["clock","时钟"]];
const OVL=[["cpu","CPU温度"],["power_cpu","CPU功率(PKG)"],["gpu","GPU温度"],["power_gpu","GPU功率"],["board","主板温度"],["cpu_use","CPU使用率"],["load","负载均值"],["pump","水泵转速"],["fan","风扇转速"],["clock","实时时钟"]];
let S=null,built=false;
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
async function jget(u){return await (await fetch(u)).json()}
async function jpost(u,b){return await (await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})})).json()}
async function refresh(){
  try{S=await jget("/api/state")}catch(e){document.getElementById("modePill").textContent="后端离线";return}
  document.getElementById("modePill").textContent=S.mode==="media"?("播放中: "+((S.media&&(S.media.title||S.media.id))||"?")):(S.mode==="screensaver"?"防烧屏屏保模式":"仪表盘模式");
  const sn=S.sensors||{};
  const chip=(l,v,u)=>`<span class="sensor">${l}<b>${v===undefined||v===null?"-":v+(u||"")}</b></span>`;
  document.getElementById("sensors").innerHTML=
    chip("CPU ",sn.cpu,"°C")+chip("GPU",sn.gpu,"°C")+chip("主板",sn.board,"°C")+
    chip("PKG ",sn.power_cpu,"W")+chip("GFX",sn.power_gpu,"W")+
    chip("水泵",sn.pump," rpm")+chip("风扇",sn.fan," rpm")+chip("负载",sn.load);
  if(!built){built=true;
    document.getElementById("switches").innerHTML=SW.map(([k,l])=>`<label><input type="checkbox" id="sw_${k}"> ${l}</label>`).join("");
    document.getElementById("ovSwitches").innerHTML=OVL.map(([k,l])=>`<label><input type="checkbox" id="ovsw_${k}"> ${l}</label>`).join("");
  }
  const ae=document.activeElement;
  if(!ae||ae.tagName!=="INPUT"){
    document.getElementById("title").value=S.dashboard.title||"";
    SW.forEach(([k])=>{document.getElementById("sw_"+k).checked=!!S.dashboard[k]});
  }
  if(!ae||!["INPUT","SELECT"].includes(ae.tagName)){
    const o=S.overlay||{};const oi=o.items||{};
    document.getElementById("ovEnabled").checked=!!o.enabled;
    document.getElementById("ovPos").value=o.pos||"br";
    document.getElementById("ovSize").value=o.size||"M";
    OVL.forEach(([k])=>{document.getElementById("ovsw_"+k).checked=!!oi[k]});
    const sv=S.screensaver||{};
    document.getElementById("savStyle").value=sv.style||"bounce";
    document.getElementById("savClock").checked=sv.clock!==false;
  }
  if(!ED)renderMedia();
}
async function renderMedia(){
  let list=[];try{list=await jget("/api/media/list")}catch(e){}
  document.getElementById("media").innerHTML=list.map(m=>`
    <div class="mitem"><img src="/media/${m.id}/thumb" loading="lazy">
    <div class="t">${esc(m.title)}</div>
    <div class="i">${m.kind} · ${m.frames} 帧${m.fps?" · "+m.fps+" fps":""}${m.duration?" · "+m.duration+"s":""}${m.crop?" · 已取景":""}</div>
    <div class="row"><button class="${(S.media&&S.media.id===m.id)?"":"sec"}" onclick="act('${m.id}')">${(S.media&&S.media.id===m.id)?"播放中":"播放"}</button>
    <button class="sec" onclick="openEd('${m.id}')">取景</button>
    <button class="sec" onclick="del('${m.id}')">删除</button></div></div>`).join("");
}
async function act(id){await jpost("/api/media/activate",{id});refresh()}
async function del(id){if(!confirm("删除该媒体？(含原始文件)"))return;await jpost("/api/media/delete",{id});refresh()}
async function applyDash(){
  const db={title:document.getElementById("title").value};
  SW.forEach(([k])=>db[k]=document.getElementById("sw_"+k).checked);
  await jpost("/api/dashboard",{dashboard:db});refresh();
}
async function savOn(){
  const msg=document.getElementById("savMsg");
  msg.textContent="已启用";
  await jpost("/api/screensaver",{style:document.getElementById("savStyle").value,
                                  clock:document.getElementById("savClock").checked});
  refresh();setTimeout(()=>msg.textContent="",2500);
}
async function applyOvl(){
  const o={enabled:document.getElementById("ovEnabled").checked,
           pos:document.getElementById("ovPos").value,
           size:document.getElementById("ovSize").value,items:{}};
  OVL.forEach(([k])=>o.items[k]=document.getElementById("ovsw_"+k).checked);
  const msg=document.getElementById("ovMsg");
  msg.textContent="已应用，播放媒体时生效";
  await jpost("/api/overlay",{overlay:o});refresh();
  setTimeout(()=>msg.textContent="",2500);
}
async function upload(){
  const el=document.getElementById("file").files[0];
  const msg=document.getElementById("upMsg");
  if(!el){msg.textContent="请先选择文件";return}
  const btn=document.getElementById("upBtn");btn.disabled=true;
  msg.textContent="上传+转码中（视频可能要几十秒）…";
  const fd=new FormData();fd.append("file",el);
  try{
    const r=await fetch("/api/media/upload",{method:"POST",body:fd});
    const j=await r.json();
    if(r.ok){msg.textContent="完成！可「取景」或直接「播放」";refresh()}
    else msg.textContent="失败: "+(j.error||r.status);
  }catch(e){msg.textContent="失败: "+e}
  btn.disabled=false;
}
/* ---------- 取景编辑器: cover 底图 + 平移/缩放, 保存方形区域 ---------- */
let ED=null;
const edEl=id=>document.getElementById(id);
function k0(){return ED.L/Math.min(ED.W,ED.H)}
function layout(){
  const e=k0()*ED.z,w=ED.W*e,h=ED.H*e,L=ED.L;
  ED.tx=Math.min(0,Math.max(ED.tx,L-w));
  ED.ty=Math.min(0,Math.max(ED.ty,L-h));
  const i=edEl("edImg");
  i.style.width=w+"px";i.style.height=h+"px";i.style.left=ED.tx+"px";i.style.top=ED.ty+"px";
  const side=(L/e);
  edEl("edInfo").textContent=`取景 ${Math.round(side)}×${Math.round(side)} / 原图 ${ED.W}×${ED.H}（缩放 ${ED.z.toFixed(2)}×，方框=屏幕 480×480）`;
  edEl("edZoom").value=Math.round(ED.z*100);
}
function setZoom(z){
  if(!ED)return;
  const L=ED.L,e=k0()*ED.z;
  const cx=(L/2-ED.tx)/e, cy=(L/2-ED.ty)/e;   // 容器中心对应的图上坐标
  ED.z=Math.min(6,Math.max(1,z));
  const e2=k0()*ED.z;
  ED.tx=L/2-cx*e2; ED.ty=L/2-cy*e2;
  layout();
}
function openEd(id){
  edMsg("加载原始图…");
  edEl("edMask").style.display="flex";
  const img=new Image();
  img.onload=()=>{
    const L=edEl("edView").clientWidth;
    ED={id,W:img.naturalWidth,H:img.naturalHeight,z:1,tx:0,ty:0,L};
    const i=edEl("edImg");i.src=img.src;
    edEl("edKind").textContent=ED.W+"×"+ED.H;
    layout();
    edMsg("");
  };
  img.onerror=()=>{edMsg("原始文件不可用")};
  img.src="/media/"+id+"/base?t="+Date.now();
}
function edClose(){ED=null;edEl("edMask").style.display="none";renderMedia()}
function edMsg(t){edEl("edMsg").textContent=t}
async function edSave(){
  if(!ED)return;
  const e=k0()*ED.z,L=ED.L;
  const x=(-ED.tx)/e/ED.W, y=(-ED.ty)/e/ED.H, w=(L/e)/ED.W;
  edMsg("处理中…（视频需重新转码，可能几十秒）");
  try{
    const r=await fetch("/api/media/crop",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:ED.id,x,y,w})});
    const j=await r.json();
    if(r.ok){edClose()}else edMsg("失败: "+(j.error||r.status));
  }catch(err){edMsg("失败: "+err)}
}
async function edReset(){
  if(!ED)return;
  edMsg("还原中…");
  const j=await jpost("/api/media/reset",{id:ED.id});
  if(j.ok){ED.z=1;ED.tx=0;ED.ty=0;layout();edMsg("已还原")}
  else edMsg("失败: "+(j.error||""));
}
(function(){
  const v=edEl("edView");let down=false,lx=0,ly=0;
  v.addEventListener("pointerdown",ev=>{down=true;lx=ev.clientX;ly=ev.clientY;v.classList.add("drag");v.setPointerCapture(ev.pointerId)});
  v.addEventListener("pointermove",ev=>{if(!down||!ED)return;ED.tx+=ev.clientX-lx;ED.ty+=ev.clientY-ly;lx=ev.clientX;ly=ev.clientY;layout()});
  v.addEventListener("pointerup",()=>{down=false;v.classList.remove("drag")});
  v.addEventListener("wheel",ev=>{if(!ED)return;ev.preventDefault();setZoom(ED.z*(ev.deltaY<0?1.12:0.89))},{passive:false});
})();
refresh();setInterval(refresh,3000);
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8085, threaded=True)
