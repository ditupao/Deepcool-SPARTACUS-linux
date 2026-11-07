#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The SPARTACUS-linux Authors
# SPDX-License-Identifier: MIT
#
# Provided for research, education and interoperability purposes only.
# Hardware interaction at your own risk; AS IS, no warranty. See LICENSE/NOTICE.
#
# spartacusd - Linux native driver for DeepCool ICE BINGYU (冰暴) series
# AIO liquid cooler LCD (USB 3633:0027, 480x480, vendor JPEG push
# protocol, independently implemented for interoperability).
# See LICENSE and NOTICE for license, purpose limitation and references.
#
# 模式: dashboard(卡片可配) | media(图片/GIF/视频) + OSD 悬浮信息层
# 协议: EP4 init -> Start(LE32 len + LE16 sum + u8 blocks) + trans*N(505B) + DCLdfinish
import glob, os, struct, sys, time, json
import usb.core, usb.util
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

INIT_CMD = bytes.fromhex(
    "aa2e0501000000000000000000000000000000000000000000000000"
    "000000000000000000000000de00")
SETTINGS_PATH = "/etc/spartacusd/settings.json"
MEDIA_ROOT = "/var/lib/spartacus/media"
DASH_FPS = 2.0
DEFAULT_DASH = {"title": "SPARTACUS · Linux", "cpu": True, "gpu": True,
                "board": True, "pump": True, "fan": True,
                "cpu_use": False, "load": True, "clock": True}
DEFAULT_OVL = {"enabled": False, "pos": "br", "size": "M",
               "items": {"cpu": True, "gpu": False, "board": False,
                         "power_cpu": True, "power_gpu": False, "cpu_use": False,
                         "load": False, "pump": False, "fan": False, "clock": False}}

_cache = {"mtime": None, "data": None}
def settings_mtime():
    try: return os.path.getmtime(SETTINGS_PATH)
    except OSError: return None

def load_settings():
    mt = settings_mtime()
    if _cache["data"] is not None and _cache["mtime"] == mt:
        return _cache["data"]
    try:
        with open(SETTINGS_PATH) as f: s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("mode", "dashboard")
    s.setdefault("media", None)
    db = s.setdefault("dashboard", {})
    for k, v in DEFAULT_DASH.items():
        db.setdefault(k, v)
    o = s.setdefault("overlay", {})
    for k, v in DEFAULT_OVL.items():
        o.setdefault(k, v)
    _cache.update(mtime=mt, data=s)
    return s

_prev = {"v": None}
def cpu_usage():
    try:
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
    except Exception:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    p = _prev["v"]
    _prev["v"] = (total, idle)
    if p is None: return None
    dt = total - p[0]
    if dt <= 0: return None
    return 100.0 * (1.0 - (idle - p[1]) / dt)

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

def read_sensors():
    hw = {}
    for p in glob.glob("/sys/class/hwmon/hwmon*"):
        try: hw[open(p+"/name").read().strip()] = p
        except Exception: pass
    d = {}
    try: d["cpu"] = int(open(hw["coretemp"]+"/temp1_input").read())/1000
    except Exception: pass
    try: d["gpu"] = int(open(hw["amdgpu"]+"/temp1_input").read())/1000
    except Exception: pass
    try: d["board"] = int(open(hw["nct6798"]+"/temp6_input").read())/1000
    except Exception: pass
    try:
        b = hw.get("nct6798")
        d["pump"] = int(open(b+"/fan2_input").read())
        d["fan"] = int(open(b+"/fan1_input").read())
    except Exception: pass
    try: d["load"] = float(open("/proc/loadavg").read().split()[0])
    except Exception: pass
    try: d["power_gpu"] = int(open(hw["amdgpu"]+"/power1_average").read())/1e6
    except Exception: pass
    w = cpu_power()
    if w is not None: d["power_cpu"] = w
    u = cpu_usage()
    if u is not None: d["cpu_use"] = u
    return d

FT = {}
def fnt(sz):
    if sz not in FT:
        f = None
        for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
            if os.path.exists(p):
                try: f = ImageFont.truetype(p, sz); break
                except Exception: pass
        FT[sz] = f if f else ImageFont.load_default()
    return FT[sz]

OSD_ITEMS = [
    ("cpu",       "CPU %.0f\u00b0C"),
    ("gpu",       "GPU %.0f\u00b0C"),
    ("board",     "SYS %.0f\u00b0C"),
    ("power_cpu", "PKG %.0fW"),
    ("power_gpu", "GFX %.0fW"),
    ("cpu_use",   "USE %.0f%%"),
    ("load",      "LOAD %.2f"),
    ("pump",      "PUMP %d"),
    ("fan",       "FAN %d"),
]
def draw_osd(img, d, ocfg):
    sz = {"S": 20, "M": 28, "L": 40}.get(str(ocfg.get("size", "M")), 28)
    items = ocfg.get("items") or {}
    lines = []
    for key, fmt in OSD_ITEMS:
        if items.get(key) and key in d:
            try: lines.append(fmt % d[key])
            except Exception: pass
    if items.get("clock"):
        lines.append(time.strftime("%H:%M:%S"))
    if not lines:
        return img
    f = fnt(sz)
    probe = ImageDraw.Draw(img)
    pad = max(8, sz // 2)
    gap = sz // 6
    line_h = sz + gap
    tw = max(probe.textlength(t, font=f) for t in lines)
    W = int(tw) + pad * 2
    H = len(lines) * line_h + pad * 2 - gap
    M = 14
    vert, hor = str(ocfg.get("pos", "br"))[0], str(ocfg.get("pos", "br"))[-1]
    x = {"l": M, "c": (480 - W) // 2, "r": 480 - W - M}.get(hor, M)
    y = {"t": M, "b": 480 - H - M}.get(vert, 480 - H - M)
    base = img.convert("RGBA")
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rounded_rectangle([x, y, x + W, y + H], radius=max(6, sz // 2), fill=(0, 0, 0, 150))
    ty = y + pad
    sw = max(1, sz // 14)
    for t in lines:
        od.text((x + pad, ty), t, fill=(255, 255, 255, 255), font=f,
                stroke_width=sw, stroke_fill=(0, 0, 0, 255))
        ty += line_h
    return Image.alpha_composite(base, ov).convert("RGB")

def render(d, db):
    img = Image.new("RGB", (480, 480), (16, 18, 32))
    dr = ImageDraw.Draw(img)
    if db.get("title"):
        dr.text((24, 30), str(db["title"])[:28], fill=(90, 200, 255),
                anchor="lm", font=fnt(26))
    if db.get("clock"):
        dr.text((456, 30), time.strftime("%H:%M:%S"), fill=(150, 152, 180),
                anchor="rm", font=fnt(22))
    dr.line([(30, 55), (450, 55)], fill=(60, 60, 90), width=2)
    y0 = 110
    if db.get("cpu") and d.get("cpu") is not None:
        c = (80, 230, 120) if d["cpu"] < 60 else ((240, 180, 40) if d["cpu"] < 80 else (250, 70, 70))
        dr.text((240, 152), "%.0f\u00b0C" % d["cpu"], fill=c, anchor="mm", font=fnt(96))
        dr.text((240, 226), "CPU PACKAGE", fill=(150, 150, 180), anchor="mm", font=fnt(20))
        y0 = 285
    defs = [
        ("gpu",     lambda: ("%.0f\u00b0C" % d["gpu"],  "GPU")),
        ("board",   lambda: ("%.0f\u00b0C" % d["board"], "SYSTEM")),
        ("power_cpu", lambda: ("%.0fW" % d["power_cpu"], "CPU POWER")),
        ("power_gpu", lambda: ("%.0fW" % d["power_gpu"], "GPU POWER")),
        ("cpu_use", lambda: ("%.0f%%" % d["cpu_use"],   "CPU USAGE")),
        ("load",    lambda: ("%.2f" % d["load"],        "LOAD AVG")),
        ("pump",    lambda: ("%d" % d["pump"],          "PUMP RPM")),
        ("fan",     lambda: ("%d" % d["fan"],           "FAN RPM")),
    ]
    slots = []
    for k, fn in defs:
        if db.get(k) and k in d:
            try:
                slots.append(fn())
            except Exception:
                pass
    for i, (v, label) in enumerate(slots[:6]):
        x = 120 if i % 2 == 0 else 360
        y = y0 + (i // 2) * 72
        dr.text((x, y), v, fill=(225, 228, 245), anchor="mm", font=fnt(34))
        dr.text((x, y + 30), label, fill=(140, 143, 170), anchor="mm", font=fnt(17))
    return img

def make_jpeg(db):
    for q in (85, 75, 65, 55, 45):
        b = BytesIO(); render(read_sensors(), db).save(b, "JPEG", quality=q)
        data = b.getvalue()
        if len(data) <= 33000000: return data
    return data

def build_frame(jpeg):
    L = len(jpeg)
    n = (L + 504) // 505
    blk0 = bytearray(512)
    blk0[0:5] = b"Start"; blk0[5] = 1
    blk0[6:10] = struct.pack("<I", L)
    blk0[10:12] = struct.pack("<H", sum(jpeg) & 0xFFFF)
    blk0[12] = n & 0xFF
    out = bytearray(blk0)
    for k in range(1, n + 1):
        blk = bytearray(512)
        blk[0:5] = b"trans"
        blk[5:7] = struct.pack("<H", k)
        chunk = jpeg[(k - 1) * 505 : k * 505]
        blk[7:7 + len(chunk)] = chunk
        out += blk
    fin = bytearray(512); fin[0:10] = b"DCLdfinish"
    out += fin
    return bytes(out)

def send_frame(ep, frame):
    for i in range(0, len(frame), 512):
        ep.write(frame[i:i + 512], timeout=3000)

def getEP(intf, num):
    return usb.util.find_descriptor(intf,
        custom_match=lambda e: (e.bEndpointAddress & 0x0f) == num and
        usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)

def connect():
    dev = usb.core.find(idVendor=0x3633, idProduct=0x0027)
    if dev is None: return None, None, None
    try:
        if dev.is_kernel_driver_active(0): dev.detach_kernel_driver(0)
        dev.set_configuration()
        intf = dev.get_active_configuration()[(0, 0)]
        ep2, ep4 = getEP(intf, 2), getEP(intf, 4)
        ep4.write(INIT_CMD, timeout=3000)
        return dev, ep2, ep4
    except Exception as e:
        print("connect error: %s" % e, flush=True)
        return None, None, None

def overlay_jpeg(jpeg, ocfg):
    if not ocfg or not ocfg.get("enabled"):
        return jpeg
    try:
        im = Image.open(BytesIO(jpeg)).convert("RGB")
        im = draw_osd(im, read_sensors(), ocfg)
        b = BytesIO(); im.save(b, "JPEG", quality=90)
        return b.getvalue()
    except Exception as e:
        print("overlay error: %s" % e, flush=True)
        return jpeg

def play_media(ep2, media_dir, mt0, ocfg):
    try:
        with open(os.path.join(media_dir, "manifest.json")) as f:
            m = json.load(f)
    except Exception as e:
        print("manifest 读取失败: %s" % e, flush=True)
        time.sleep(2); return
    frames = int(m.get("frames") or 0)
    fps = float(m.get("fps") or 12)
    durs = m.get("durations")
    print("播放媒体: %s (%s, %d 帧, %g fps, overlay=%s)" % (
        m.get("title", "?"), m.get("kind"), frames, fps,
        "on" if (ocfg and ocfg.get("enabled")) else "off"), flush=True)
    for k in range(1, frames + 1):
        if settings_mtime() != mt0: return
        try:
            jpeg = open(os.path.join(media_dir, "f%05d.jpg" % k), "rb").read()
        except OSError:
            return
        jpeg = overlay_jpeg(jpeg, ocfg)
        send_frame(ep2, build_frame(jpeg))
        dt = (durs[k - 1] / 1000.0) if durs and k <= len(durs) else (1.0 / fps)
        time.sleep(min(max(dt, 0.04), 3.0))

def preview(path):
    """python3 spartacusd.py preview out.jpg —— 按当前设置出一帧效果图"""
    read_sensors(); time.sleep(0.35)
    d = read_sensors()
    s = load_settings()
    if s["mode"] == "media" and s.get("media"):
        f0 = os.path.join(MEDIA_ROOT, os.path.basename(str(s["media"])), "f00001.jpg")
        if os.path.exists(f0):
            im = Image.open(f0).convert("RGB")
            o = s.get("overlay") or {}
            if o.get("enabled"):
                im = draw_osd(im, d, o)
            im.save(path, "JPEG", quality=90)
            print("preview(media):", path); return
    render(d, s["dashboard"]).save(path, "JPEG", quality=90)
    print("preview(dashboard):", path)

def main():
    print("spartacusd v3 starting", flush=True)
    dev = ep2 = ep4 = None
    last_jpeg = last_frame = None
    while True:
        if ep2 is None:
            dev, ep2, ep4 = connect()
            if ep2 is None:
                time.sleep(3); continue
            print("设备已连接，会话建立", flush=True)
        s = load_settings()
        mt0 = settings_mtime()
        try:
            if s["mode"] == "media" and s.get("media"):
                play_media(ep2, os.path.join(MEDIA_ROOT, os.path.basename(str(s["media"]))),
                           mt0, s.get("overlay"))
            else:
                jpeg = make_jpeg(s["dashboard"])
                if jpeg != last_jpeg:
                    last_frame = build_frame(jpeg); last_jpeg = jpeg
                send_frame(ep2, last_frame)
                time.sleep(1.0 / DASH_FPS)
        except Exception as e:
            print("写异常，重连: %s" % e, flush=True)
            ep2 = ep4 = None

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preview":
        preview(sys.argv[2])
    else:
        main()
