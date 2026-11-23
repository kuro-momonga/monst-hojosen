"""
モンスト補助線ツール用 BlueStacks ヘルパー（PCで動かす小さなサーバー）

  - ツールで決めた角度で BlueStacks に引っ張り操作を送る（adb shell input swipe）
  - BlueStacks の画面をそのまま撮ってツールに渡す（adb exec-out screencap）

使い方:
  1. BlueStacks の 設定 → 詳細設定 →「Android デバッグブリッジ（ADB）」をオンにする
     （そこに表示されるポート番号、ふつうは 5555）
  2. このファイルと同じフォルダの start_helper.bat をダブルクリック
     （または: python monst_helper.py --port 5555）
  3. ツールの「🎯 BlueStacks」から接続する

必要なもの: Python 3（標準ライブラリだけで動きます）、adb（BlueStacks 付属の HD-Adb.exe を自動で探します）
"""
import re
import argparse, json, math, os, random, shutil, struct, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = 8765
HELPER_VER = 114
CANDIDATES = [
    r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe",
    r"C:\Program Files\BlueStacks\HD-Adb.exe",
    r"C:\Program Files (x86)\BlueStacks_nxt\HD-Adb.exe",
]


def find_adb():
    for c in ("adb", "adb.exe"):
        p = shutil.which(c)
        if p:
            return p
    for c in CANDIDATES:
        if os.path.exists(c):
            return c
    return None


class Adb:
    def __init__(self, exe, serial):
        self.exe, self.serial = exe, serial
        self.size = None

    def run(self, *args, binary=False, timeout=15):
        cmd = [self.exe, "-s", self.serial, *args]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"exit {r.returncode}: " + ((r.stderr or r.stdout).decode(errors="ignore").strip()[:200] or "adb error"))
        return r.stdout if binary else r.stdout.decode(errors="ignore")

    def connect(self):
        subprocess.run([self.exe, "connect", self.serial], capture_output=True, timeout=10)
        out = self.run("shell", "wm", "size")
        # "Physical size: 1080x1920" (+ maybe "Override size: ...")
        sizes = [l.split(":")[1].strip() for l in out.splitlines() if "size" in l]
        w, h = map(int, sizes[-1].split("x"))
        self.natural = (w, h)            # size in the panel's natural orientation (touch coordinates follow this)
        self.rot = self.rotation()
        self.size = (h, w) if self.rot in (1, 3) else (w, h)   # what the app actually sees (input swipe / screenshots)
        return self.size

    def rotation(self):
        for cmd in (["dumpsys", "input"], ["dumpsys", "window", "displays"]):
            try:
                out = self.run("shell", *cmd, timeout=10)
            except Exception:
                continue
            for key in ("SurfaceOrientation:", "mCurrentRotation=", "mRotation=", "rotation="):
                i = out.find(key)
                if i >= 0:
                    v = out[i + len(key):i + len(key) + 12].strip().split()[0].strip(",")
                    v = {"ROTATION_0": "0", "ROTATION_90": "1", "ROTATION_180": "2", "ROTATION_270": "3"}.get(v, v)
                    if v.isdigit():
                        return int(v) % 4
        return 0

    def raw_turn(self):
        """Whose turn it is (1..4), from the bottom panel: the turn's portrait is raised, so the orange gap under it is missing.
        0 = nobody (enemy turn / animations). Raw screencap (no PNG encode) analysed with plain Python."""
        data = self.run("exec-out", "screencap", binary=True, timeout=8)
        if len(data) < 16:
            return 0
        w, h = struct.unpack_from("<II", data, 0)
        off = len(data) - w * h * 4
        if w <= 0 or h <= 0 or off < 12 or off > 64:
            data = data.replace(b"\r\n", b"\n"); off = len(data) - w * h * 4
            if off < 12 or off > 64:
                return 0
        self._last = (data, off, w, h, time.time())   # /screen can hand this very frame over right away
        self.next_turn = next_turn_rgba(data, off, w, h)
        return turn_from_rgba(data, off, w, h)

    def raw_frame(self):
        data = self.run("exec-out", "screencap", binary=True, timeout=8)
        w, h = struct.unpack_from("<II", data, 0)
        off = len(data) - w * h * 4
        if off < 12 or off > 64:
            data = data.replace(b"\r\n", b"\n"); off = len(data) - w * h * 4
        if off < 12 or off > 64:
            raise RuntimeError("raw screencap")
        return data, off, w, h

    def screen_bmp(self, max_age=2.0):
        """the screen as a BMP (no PNG encoding on the emulator: much faster). Reuses the watcher's frame when fresh."""
        last = getattr(self, "_last", None)
        if last and time.time() - last[4] <= max_age:
            data, off, w, h, _ = last
        else:
            data, off, w, h = self.raw_frame()
        px = bytearray(data[off:off + w * h * 4])
        px[0::4], px[2::4] = px[2::4], px[0::4]          # RGBA -> BGRA
        hdr = struct.pack("<2sIHHI", b"BM", 54 + len(px), 0, 0, 54) + struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, len(px), 2835, 2835, 0, 0)
        return hdr + bytes(px)

    def tap(self, x, y, seed=None):
        """a click-like tap: finger down ~50 ms. The down / up events are written to files on the emulator first,
        then one shell command writes them to the touch device with only a short sleep between
        (streaming through adb or many sendevent calls was slow enough to count as a long press)"""
        rng = random.Random(seed)
        if not self.touch():
            self.run("shell", "input", "tap", str(round(x)), str(round(y)), timeout=10); return "input"
        if not hasattr(self, "_evsize"):
            abi = self.run("shell", "getprop", "ro.product.cpu.abi").strip()
            self._evsize = 24 if "64" in abi else 16
        fmt = "<qqHHi" if self._evsize == 24 else "<iiHHi"
        pk = lambda evs: b"".join(struct.pack(fmt, 0, 0, a, b_, c) for a, b_, c in evs)
        conv = self.conv_fn(); d = self._touch["dev"]
        x += rng.uniform(-3, 3); y += rng.uniform(-3, 3)
        X, Y = conv(x, y)
        # like a real mouse click on BlueStacks: the position twice, then the release ~30 ms later
        # (the event files go over with adb push)
        import tempfile
        tag = self.serial.replace(":", "_").replace(".", "_")
        for name, data in (("monst_d.bin", pk(self.E_down(X, Y)) * 2), ("monst_u.bin", pk(self.E_up()))):
            loc = os.path.join(tempfile.gettempdir(), f"{tag}_{name}")
            with open(loc, "wb") as fh:
                fh.write(data)
            self.run("push", loc, f"/data/local/tmp/{name}", timeout=15)   # plain adb push: works everywhere
        self.unstick()
        self.run("shell", f"cat /data/local/tmp/monst_d.bin > {d}; sleep {rng.uniform(.03, .05):.3f}; cat /data/local/tmp/monst_u.bin > {d}", timeout=10)
        return "tap"

    def screencap(self):
        data = self.run("exec-out", "screencap", "-p", binary=True)
        if not data.startswith(b"\x89PNG"):  # some builds turn \n into \r\n
            data = data.replace(b"\r\n", b"\n")
        return data

    def shoot(self, angle_deg, cx, cy, ms, hold=400, wander=False, method="auto", seed=None):
        """Pull opposite to angle_deg (0 = right, clockwise +, screen y down) from (cx, cy).
        Picks the integer end point (pull length 0.3..0.42 of the width) whose direction is closest to the angle."""
        rng = random.Random(seed)   # same seed on every synced instance -> the very same movement
        w, h = self.size
        a = math.radians(angle_deg)
        dx, dy = -math.cos(a), -math.sin(a)
        best = None
        # random pull length every shot (the arrow size changes), then the integer point closest to the angle near it
        Lc = rng.uniform(.15, .70) * w
        cx = round(cx + rng.uniform(-.04, .04) * w); cy = round(cy + rng.uniform(-.03, .03) * h)   # press point varies a bit too
        # if the long pull would leave the screen, shorten it until it fits
        room = min([(w - 6 - cx) / dx if dx > 0 else (cx - 6) / -dx if dx < 0 else 1e9,
                    (h - 6 - cy) / dy if dy > 0 else (cy - 6) / -dy if dy < 0 else 1e9])
        Lc = max(.12 * w, min(Lc, room - 14))
        for L in range(int(Lc - 12), int(Lc + 13)):
            ex, ey = round(cx + dx * L), round(cy + dy * L)
            if not (5 <= ex <= w - 5 and 5 <= ey <= h - 5):
                continue
            err = abs(((math.degrees(math.atan2(cy - ey, cx - ex)) - angle_deg + 180) % 360) - 180)
            if best is None or err < best[0]:
                best = (err, ex, ey)
        if best is None:
            raise RuntimeError("引っ張る先が画面からはみ出します")
        err, ex, ey = best
        sx, sy = round(cx), round(cy)
        # path: list of (x, y, wait_s). Touch down at (sx, sy), release at (ex, ey): only these two decide the shot.
        path = []
        if wander:
            # human-like aiming: quick first pull roughly toward the aim, then a few corrections that get smaller
            # (sometimes overshooting to the other side), sometimes one wide "look around", pauses to think,
            # each movement with a bell-shaped (minimum-jerk) speed profile
            base = math.atan2(ey - sy, ex - sx); L0 = math.hypot(ex - sx, ey - sy)
            def at(ang, L):
                return (min(w - 5, max(5, sx + math.cos(ang) * L)), min(h - 5, max(5, sy + math.sin(ang) * L)))
            def move(a0, L0_, a1, L1, dur):
                n = max(3, round(dur / .008))
                for i in range(1, n + 1):
                    u = i / n; u = u ** 3 * (10 - 15 * u + 6 * u * u)       # minimum jerk
                    path.append((*at(a0 + (a1 - a0) * u, L0_ + (L1 - L0_) * u), dur / n))
            def pause(t):
                path.append((*path[-1][:2], t)) if path else None
            err = math.radians(rng.uniform(10, 25)) * rng.choice((1, -1))   # stay within ±25° of the real aim
            ca, cL = base + err, L0 * rng.uniform(.35, 1.3)
            move(base + err, L0 * .1, ca, cL, rng.uniform(.9, 1.6))   # first pull: unhurried
            pause(rng.uniform(.15, .5))
            if rng.random() < .45:   # look the other way once (still within ±25°)
                la = base + math.radians(rng.uniform(12, 25)) * (-1 if err > 0 else 1); lL = L0 * rng.uniform(.6, 1.1)
                move(ca, cL, la, lL, rng.uniform(.4, .8)); pause(rng.uniform(.2, .6))
                ca, cL = la, lL
            for k in range(rng.randint(3, 6)):
                err *= -rng.uniform(.25, .6) if rng.random() < .5 else rng.uniform(.25, .6)
                na, nL = base + err, L0 * rng.uniform(.4, 1.4)        # the arrow grows and shrinks a lot
                if rng.random() < .3: na = ca                          # sometimes only pull in / out
                d = abs((na - ca + math.pi) % (2 * math.pi) - math.pi) + abs(nL - cL) / max(L0, 1) * .6
                speed = rng.choice([rng.uniform(.35, .7), rng.uniform(.8, 1.3), rng.uniform(1.5, 2.6)])   # quick / normal / slow
                move(ca, cL, ca + ((na - ca + math.pi) % (2 * math.pi) - math.pi), nL, (.15 + d * .9) * speed)
                ca, cL = na, nL
                pause(rng.choice([rng.uniform(.05, .2), rng.uniform(.2, .6), rng.uniform(.6, 1.1)]))
            move(ca, cL, base, L0, rng.uniform(.25, .5))
            path.append((ex, ey, rng.uniform(.1, .35)))
        else:
            n = max(6, round(ms / 1000 / .008))
            for i in range(1, n + 1):
                t = i / n; t = t * t * (3 - 2 * t)
                path.append((sx + (ex - sx) * t, sy + (ey - sy) * t, ms / 1000 / n))
        if wander:
            # settle like a hand: overshoot a little, then 2-3 small corrections onto the aim
            ox, oy = rng.gauss(0, 9), rng.gauss(0, 9)
            for k in range(rng.randint(2, 3)):
                f = (.45 ** k) * rng.choice((1, -1)) if k else 1
                tx, ty = ex + ox * f, ey + oy * f
                lx, ly = path[-1][0], path[-1][1]
                n = rng.randint(12, 30)
                for i in range(1, n + 1):
                    u = i / n; u = u * u * (3 - 2 * u)
                    path.append((lx + (tx - lx) * u, ly + (ty - ly) * u, rng.uniform(.006, .012)))
            lx, ly = path[-1][0], path[-1][1]
            for i in range(1, 16):
                u = i / 15; path.append((lx + (ex - lx) * u, ly + (ey - ly) * u, .01))
            # hand noise: tremor (8-12 Hz, ~2 px) + small random jitter + uneven frame timing + micro pauses
            f1, f2 = rng.uniform(8, 12), rng.uniform(3, 6)
            a1, a2 = rng.uniform(1, 2.5), rng.uniform(1, 3)
            f3, a3 = rng.uniform(15, 25), rng.uniform(.6, 1.4)     # fine, fast finger shake
            ph = [rng.uniform(0, 6.3) for _ in range(6)]
            t, out = 0.0, []
            fade_from = len(path) - 15
            # noise mostly while moving: gain follows the (smoothed) finger speed, plus random calm spells with none
            gs, calm_until, px_, py_ = 0.0, -1.0, path[0][0], path[0][1]
            for idx, (x, y, dt) in enumerate(path):
                t += dt
                v = math.hypot(x - px_, y - py_) / max(dt, 1e-3); px_, py_ = x, y
                gs += (min(1.0, v / 900) - gs) * min(1.0, dt / .08)
                if t > calm_until and rng.random() < dt * .8:   # about once a second: a still, clean moment
                    calm_until = t + rng.uniform(.25, .9)
                g = 0 if idx >= fade_from or t < calm_until else gs
                nx = a1 * math.sin(2 * math.pi * f1 * t + ph[0]) + a2 * math.sin(2 * math.pi * f2 * t + ph[1]) + a3 * math.sin(2 * math.pi * f3 * t + ph[4]) + rng.gauss(0, 1.0)
                ny = a1 * math.sin(2 * math.pi * f1 * t + ph[2]) + a2 * math.sin(2 * math.pi * f2 * t + ph[3]) + a3 * math.sin(2 * math.pi * f3 * t + ph[5]) + rng.gauss(0, 1.0)
                dt2 = dt * rng.uniform(.75, 1.25)
                if g > .3 and rng.random() < .004:
                    dt2 += rng.uniform(.05, .2)   # tiny hesitation
                out.append((min(w - 5, max(5, x + g * nx)), min(h - 5, max(5, y + g * ny)), dt2))
            path = out
        if wander:   # same moves, played fast: the whole aiming takes about 0.65-0.95 s
            T = sum(dt for _, _, dt in path) or 1
            K = rng.uniform(.65, .95) / T
            path = [(x, y, dt * K) for x, y, dt in path]
        path.append((ex, ey, hold / 1000))

        how, errs = None, []
        # debug: record what the kernel really receives on the touch device during the shot
        mon = None
        if self.touch() and os.environ.get("MONST_DEBUG"):
            try:   # record on the emulator itself (no adb buffering), read it back afterwards
                self.run("shell", f"pkill -f 'getevent -lt' ; (getevent -lt {self._touch['dev']} > /data/local/tmp/monst_ge.txt 2>&1 &) ; sleep 0.3; true", timeout=10)
                mon = True
            except Exception as e:
                print("getevent start failed:", e)
        if self.touch() and method != "swipe":
            self.unstick()
            try:
                if method == "script":
                    self.send_touch_script(sx, sy, path[::4] + [path[-1]]); how = "script"
                elif method == "frames":
                    self.send_touch_frames(sx, sy, path); how = "frames"
                else:
                    # smooth: stream (one process on the emulator, paced from here at ~120 fps)
                    self.send_touch(sx, sy, path); how = "stream"
            except Exception as e:
                errs.append(f"sendevent: {e}")
        if how is None:
            self.run("shell", "input", "swipe", str(sx), str(sy), str(ex), str(ey), str(int(ms + hold)), timeout=30)
            how = "swipe"
        if mon:
            time.sleep(1.0)
            try:
                raw = self.run("shell", "pkill -f 'getevent -lt'; sleep 0.2; cat /data/local/tmp/monst_ge.txt; echo ---PS---; ps -A | grep -E ' (dd|getevent)$'; true", timeout=15)
            except Exception as e:
                raw = f"read failed: {e}"
            body, _, ps = raw.partition("---PS---")
            lines = [l for l in body.splitlines() if l.strip()]
            print("--- leftover processes:", " / ".join(l.strip() for l in ps.splitlines() if l.strip()) or "none")
            keys = [l for l in lines if "TRACKING_ID" in l or "BTN_TOUCH" in l]
            print(f"--- touch log: {len(lines)} events. down/up:")
            for l in keys[-8:]: print("   ", l.strip())
            print("--- last events:")
            for l in lines[-6:]: print("   ", l.strip())
        conv = self.conv_fn() if self._touch else None
        return {"from": [sx, sy], "to": [ex, ey], "err_deg": round(err, 4), "how": how, "notes": errs,
                "dev_from": conv(sx, sy) if conv else None, "dev_to": conv(ex, ey) if conv else None}

    # ---- raw touch through the emulator's touchscreen device (sendevent): a real held drag ----
    def touch(self):
        if getattr(self, "_touch", 0) != 0:
            return self._touch
        self._touch = None
        try:
            out = self.run("shell", "getevent", "-pl", timeout=10)
        except Exception:
            return None
        dev, name, info = None, "", {}
        for line in out.splitlines() + ["add device 0: /end"]:
            if line.startswith("add device"):
                if dev and "x" in info and "y" in info:
                    self._touch = {"dev": dev, "name": name, **info}
                    if "touch" in name.lower():
                        break
                dev, name, info = line.split(":", 1)[1].strip(), "", {}
            elif "name:" in line:
                name = line.split("name:", 1)[1].strip().strip('"')
            elif "ABS_MT_POSITION_X" in line or "ABS_MT_POSITION_Y" in line:
                k = "x" if "_X" in line else "y"
                try:
                    lo = int(line.split("min")[1].split(",")[0].strip()); hi = int(line.split("max")[1].split(",")[0].strip())
                    info[k] = (lo, hi)
                except Exception:
                    pass
            elif "ABS_MT_TRACKING_ID" in line:
                info["tid"] = True
        return self._touch

    def send_touch(self, sx, sy, path):
        self._sent_all = False
        try:
            return self.send_touch_stream(sx, sy, path)
        except Exception as e:
            if self._sent_all:   # the drag already went through: don't shoot twice
                print("stream warning:", e); return
            print("stream failed, falling back to script:", e)
            return self.send_touch_script(sx, sy, path)

    def conv_fn(self):
        """screen (rotated, what the app sees) -> raw touch panel coordinates (natural orientation)"""
        t = self._touch; Wn, Hn = self.natural; r = getattr(self, "rot_override", None)
        r = self.rot if r is None else r
        (x0, x1), (y0, y1) = t["x"], t["y"]
        def conv(x, y):
            if r == 1:   nx, ny = Wn - y, x
            elif r == 2: nx, ny = Wn - x, Hn - y
            elif r == 3: nx, ny = y, Hn - x
            else:        nx, ny = x, y
            return round(x0 + nx / (Wn - 1) * (x1 - x0)), round(y0 + ny / (Hn - 1) * (y1 - y0))
        return conv

    def send_touch_stream(self, sx, sy, path):
        """Smooth (~60 fps): one `cat > /dev/input/eventX` on the emulator, raw input_event structs streamed and paced from here."""
        if not hasattr(self, "_evsize"):
            abi = self.run("shell", "getprop", "ro.product.cpu.abi").strip()
            self._evsize = 24 if "64" in abi else 16
        fmt = "<qqHHi" if self._evsize == 24 else "<iiHHi"
        pk = lambda a, b, c: struct.pack(fmt, 0, 0, a, b, c)
        conv = self.conv_fn(); d = self._touch["dev"]
        # dd regroups whatever arrives into whole events (obs = event size): the kernel rejects a write that splits an event,
        # which used to kill the stream mid-drag (finger left down at a random angle, no release)
        es = self._evsize
        p = subprocess.Popen([self.exe, "-s", self.serial, "exec-in", f"dd of={d} ibs=4096 obs={es}"], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        def send(*evs):
            p.stdin.write(b"".join(pk(*e) for e in evs)); p.stdin.flush()
        X, Y = conv(sx, sy)
        t0 = time.perf_counter(); due = 0.0
        send(*self.E_down(X, Y))
        for x, y, wait in path:
            due += wait
            X, Y = conv(x, y)
            send(*self.E_move(X, Y))
            sl = t0 + due - time.perf_counter()
            if sl > 0: time.sleep(sl)
        # end inside the same stream (so nothing can overtake it): exact aim point, hold, release
        ex_, ey_, _ = path[-1]; X, Y = conv(ex_, ey_)
        send(*self.E_move(X, Y)); time.sleep(.12)
        send(*self.E_move(X, Y)); time.sleep(.12)
        send(*self.E_up())
        self._sent_all = True
        for _ in range(4):          # harmless empty SYN reports: push the tail through any buffering
            send(*[(0, 0, 0)] * 100); time.sleep(.05)
        time.sleep(.6)             # let adb deliver what it can
        p.stdin.close()
        try:
            p.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            p.kill()
        err = (p.stderr.read() or b"").decode(errors="ignore").strip()
        if err: print("dd:", err.replace("\n", " | ")[:200])
        # the stream process is gone now, so nothing can overtake this: make sure the finger is lifted
        # at the exact aim point (the tail of the stream can get stuck in buffers and never arrive)
        ev = lambda a, b, c: f"sendevent {d} {a} {b} {c}"
        self.run("shell", "; ".join([ev(*e) for e in self.E_up()]), timeout=15)
        if any(k in err for k in ("nvalid", "denied", "not found", "rror")):
            raise RuntimeError(err[:200])

    # ---- touch protocol: BlueStacks' virtual touch is multitouch protocol A (no tracking id / BTN_TOUCH):
    #      each frame = X, Y, SYN_MT_REPORT, SYN_REPORT; lifting = an empty report (SYN_MT_REPORT, SYN_REPORT)
    def protoB(self):
        return bool(self._touch and self._touch.get("tid"))

    def E_down(self, X, Y):
        if self.protoB():
            return [(3, 47, 0), (3, 57, self.new_tid()), (3, 53, X), (3, 54, Y), (1, 330, 1), (0, 0, 0)]
        return [(3, 53, X), (3, 54, Y), (0, 2, 0), (0, 0, 0)]

    def E_move(self, X, Y):
        return [(3, 53, X), (3, 54, Y), (0, 0, 0)] if self.protoB() else [(3, 53, X), (3, 54, Y), (0, 2, 0), (0, 0, 0)]

    def E_up(self):
        return [(3, 57, -1), (1, 330, 0), (0, 0, 0)] if self.protoB() else [(0, 2, 0), (0, 0, 0)]

    def new_tid(self):
        # a new tracking id every gesture: the kernel drops a value equal to the current one, so reusing "1"
        # after a stuck touch meant Android never saw a fresh press / a clean release
        self._tid = (getattr(self, "_tid", random.randint(100, 30000)) + 1) % 60000 or 1
        return self._tid

    def unstick(self):
        """lift a finger left down by an earlier failed attempt"""
        try:
            d = self._touch["dev"]
            self.run("shell", "; ".join(f"sendevent {d} {a} {b} {c}" for a, b, c in self.E_up()) + "; true", timeout=10)
        except Exception:
            pass

    def send_touch_frames(self, sx, sy, path):
        """All touch events go into one binary file on the emulator; a script writes them frame by frame
        with dd (one process per frame instead of three sendevent calls) -> smooth enough, and the release
        is in the same ordered script, so it always arrives."""
        import tempfile
        if not hasattr(self, "_evsize"):
            abi = self.run("shell", "getprop ro.product.cpu.abi; true").strip()
            self._evsize = 24 if "64" in abi else 16
        es = self._evsize; fmt = "<qqHHi" if es == 24 else "<iiHHi"
        pk = lambda a, b, c: struct.pack(fmt, 0, 0, a, b, c)
        conv = self.conv_fn(); d = self._touch["dev"]
        # resample the path to ~22 ms frames (a dd call takes about that long itself)
        frames, acc, wait = [], 0.0, 0.0
        for i, (x, y, dt) in enumerate(path):
            acc += dt
            if acc >= .022 or i == len(path) - 1:
                frames.append((x, y, acc)); acc = 0.0
        blob, lines, idx = bytearray(), [], 0
        def frame(*evs):
            nonlocal idx
            for e in evs: blob.extend(pk(*e))
            lines.append(f"dd if=/data/local/tmp/monst_touch.bin of={d} bs={es} skip={idx} count={len(evs)} 2>/dev/null")
            idx += len(evs)
        X, Y = conv(sx, sy)
        frame(*self.E_down(X, Y))
        for x, y, dt in frames:
            X, Y = conv(x, y)
            frame(*self.E_move(X, Y))
            wait += dt - .022
            if wait > .03:
                lines.append(f"sleep {wait:.2f}"); wait = 0.0
        frame(*self.E_up())
        files = []
        try:
            with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as f:
                f.write(bytes(blob)); files.append(f.name)
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as f:
                f.write("\n".join(lines) + "\n"); files.append(f.name)
            self.run("push", files[0], "/data/local/tmp/monst_touch.bin", timeout=20)
            self.run("push", files[1], "/data/local/tmp/monst_touch.sh", timeout=20)
        finally:
            for f in files: os.unlink(f)
        out = self.run("shell", "sh /data/local/tmp/monst_touch.sh; true", timeout=60)
        if "denied" in out.lower() or "not found" in out:
            raise RuntimeError(out.strip()[:200])

    def send_touch_script(self, sx, sy, path):
        d = self._touch["dev"]; conv = self.conv_fn()
        ev = lambda a, b, c: f"sendevent {d} {a} {b} {c}"
        X, Y = conv(sx, sy)
        cmds = [ev(*e) for e in self.E_down(X, Y)]
        pend = 0.0
        for x, y, p in path:
            X, Y = conv(x, y)
            cmds += [ev(*e) for e in self.E_move(X, Y)]
            pend += p - .015   # each sendevent line costs a little time itself
            if pend > .02:
                cmds.append(f"sleep {pend:.2f}"); pend = 0.0
        cmds += [ev(*e) for e in self.E_up()]
        # a long drag is hundreds of commands: too long for one command line -> push a script file and run it
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as f:
            f.write("\n".join(cmds) + "\n"); local = f.name
        try:
            self.run("push", local, "/data/local/tmp/monst_drag.sh", timeout=20)
        finally:
            os.unlink(local)
        out = self.run("shell", "sh /data/local/tmp/monst_drag.sh 2>&1", timeout=60)
        if "rror" in out or "denied" in out.lower() or "not found" in out:
            raise RuntimeError(out.strip()[:200])


def turn_from_rgba(data, off, w, h):
    u = w / 1170
    if w > h:
        return 0
    st = 4 * 6
    P = -1
    for y in range(h // 2, h):
        o = off + y * w * 4
        r, g, b = data[o:o + w * 4:st], data[o + 1:o + w * 4:st], data[o + 2:o + w * 4:st]
        n = sum(1 for R, G, B in zip(r, g, b) if R > 200 and 140 < G < 220 and B < 80)
        if n > len(r) * .5:
            P = y; break
    if P < 0:
        return 0
    v = []
    for k in range(4):
        n = t = 0
        x0, x1 = round((110 + 220 * k) * u), round((230 + 220 * k) * u)
        for y in range(round(P + 68 * u), min(h, round(P + 84 * u)), 2):
            o = off + y * w * 4
            seg = data[o + x0 * 4:o + x1 * 4]
            for i in range(0, len(seg) - 3, 12):
                R, G, B = seg[i], seg[i + 1], seg[i + 2]; t += 1
                if R > 200 and 110 < G < 210 and B < 90: n += 1
        v.append(n / t if t else 1)
    o = sorted(range(4), key=lambda i: v[i])
    return o[0] + 1 if v[o[0]] < .6 and v[o[1]] - v[o[0]] > .25 else 0


def next_turn_rgba(data, off, w, h):
    """the blue "NEXT TURN" banner across the field: shown right after the enemy turn
    (revives are done, the layout is final), ~0.5 s before the next portrait rises"""
    if w > h:
        return False
    x0, x1 = int(w * .08), int(w * .92); st = 8; rows = 0; best = 0.0
    for y in range(int(h * .22), int(h * .62), 2):
        o = off + y * w * 4
        seg = data[o + x0 * 4:o + x1 * 4]
        r, g, b = seg[0::st], seg[1::st], seg[2::st]
        n = sum(1 for R, G, B in zip(r, g, b) if B > 200 and R < 120 and G > 80 and B - R > 110)
        f = n / max(len(r), 1)
        if f > .1: rows += 1
        if f > best: best = f
    return best >= .3 and rows >= 8


class Watcher:
    """Looks at every instance about twice a second while the tool asks for it (auto read).
    A turn starts when a portrait rises: fire once per rise (after the portraits were down, or another one rose)."""
    def __init__(self, adbs):
        import threading
        self.adbs, self.cond = adbs, threading.Condition()
        self.id, self.ev, self.last_req, self.busy, self.last_fire = 0, None, 0, 0, (0, 0)
        self.inst = [{"serial": A.serial, "k": 0, "raw": -1, "zeros": 9, "fired": 0, "ok": False, "err": ""} for A in adbs]
        for i, A in enumerate(adbs):
            threading.Thread(target=self.loop, args=(i, A), daemon=True).start()

    def loop(self, i, A):
        st = self.inst[i]
        while True:
            if time.time() - self.last_req > 25 or time.time() < self.busy:
                time.sleep(.4); continue
            try:
                if not A.size:
                    A.connect()
                k = A.raw_turn(); st["ok"] = True; st["err"] = ""
            except Exception as e:
                st["ok"] = False; st["err"] = str(e)[:120]; time.sleep(2); continue
            if k == 0 and getattr(A, "next_turn", False) and not st.get("nt"):
                # NEXT TURN: fire now with the expected next character; the portrait that rises next is then the same turn
                nk = (st["fired"] % 4) + 1 if st["fired"] else 0
                st["nt"] = True; st["zeros"] = 0; st["fired"] = nk; st["nt_k"] = nk
                with self.cond:
                    t, kk = self.last_fire
                    if not (time.time() - t < 2.5):
                        self.last_fire = (time.time(), nk)
                        self.id += 1; self.ev = {"id": self.id, "i": i, "k": nk, "t": time.time(), "next": True}
                        print(f"NEXT TURN（{A.serial}）→ 取り込み（次は {ORD.get(nk, '?')} のはず）")
                        self.cond.notify_all()
            if k:
                st["nt"] = False
            prev, st["raw"] = st["raw"], k
            if k != prev:          # needs the same reading twice in a row
                time.sleep(.05); continue
            if k == 0:
                st["zeros"] += 1
            else:
                fire = (st["zeros"] >= 2 and not st.get("nt_k")) or k != st["fired"]
                st["nt_k"] = 0
                st["zeros"] = 0
                if fire:
                    st["fired"] = k
                    with self.cond:
                        t, kk = self.last_fire
                        if not (kk == k and time.time() - t < 5):   # the other instances see the same rise
                            self.last_fire = (time.time(), k)
                            self.id += 1; self.ev = {"id": self.id, "i": i, "k": k, "t": time.time()}
                            print(f"ターン開始: {ORD.get(k, k)}（{A.serial}）→ ツールに取り込みを依頼")
                            self.cond.notify_all()
            st["k"] = k
            time.sleep(.15)

    def wait(self, since, timeout=20):
        self.last_req = time.time()
        with self.cond:
            if since >= 0:
                self.cond.wait_for(lambda: self.id > since, timeout=timeout)
            self.last_req = time.time()
            return {"ok": True, "id": self.id, "ev": self.ev if since >= 0 and self.id > since else None,
                    "inst": [{k: v for k, v in s.items() if k in ("serial", "k", "ok", "err")} for s in self.inst]}


ORD = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def make_handler(adbs):
    adb = adbs[0]
    W = Watcher(adbs)
    SH = Shuka(adbs)
    AU = ShukaAuto(SH, adbs)
    threading.Thread(target=phone_pusher, args=(AU, SH), daemon=True).start()
    class H(BaseHTTPRequestHandler):
        def cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def reply(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.cors()
            self.end_headers()

        def do_GET(self):
            try:
                if self.path.startswith("/status"):
                    w, h = adb.connect()
                    others = []
                    for A in adbs[1:]:
                        try: A.connect(); A.touch(); others.append(A.serial)
                        except Exception: pass
                    return self.reply(200, {"ok": True, "w": w, "h": h, "serial": adb.serial, "ver": HELPER_VER, "touch": adb.touch(), "rot": adb.rot, "natural": adb.natural, "sync": others})
                if self.path.rstrip("/") == "/shuka":
                    return self.reply(200, SHUKA_PAGE.encode(), "text/html; charset=utf-8")
                if self.path.startswith("/shuka/debug"):
                    import html as _h
                    out = ["<!DOCTYPE html><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>書庫 デバッグ</title>",
                           "<body style='background:#111;color:#eee;font-family:sans-serif;margin:12px'><h2>一覧の読み取り（スクショを送ってください）</h2>"]
                    for serial, d in AU.dbg.items():
                        out.append(f"<h3>{_h.escape(serial)}</h3>")
                        pk = d.get("pick")
                        if pk:
                            out.append(f"<p>押したグループ：{_h.escape(str(pk['group']))}（{pk['time']}）　読めた順：{_h.escape(' / '.join(pk['order']))}</p>"
                                       f"<div style='position:relative;display:inline-block'><img src='data:image/png;base64,{pk['png']}' style='width:360px;display:block'>"
                                       f"<div style='position:absolute;left:0;right:0;top:{pk['head']*100:.1f}%;border-top:2px solid #3cf'></div>"
                                       f"<div style='position:absolute;left:calc({pk['x']*100:.1f}% - 9px);top:calc({pk['y']*100:.1f}% - 9px);width:14px;height:14px;border:3px solid red;border-radius:50%'></div></div>")
                        for sn in d.get("survey", []):
                            out.append(f"<div style='display:flex;gap:8px;align-items:center;margin:2px 0'><img src='data:image/png;base64,{sn['png']}' style='width:240px'>"
                                       f"<span>{sn['t']}秒 → {_h.escape(' / '.join(sn['names']) or '（読めず）')}</span></div>")
                    return self.reply(200, "".join(out).encode(), "text/html; charset=utf-8")
                if self.path.startswith("/shuka/state"):
                    return self.reply(200, {**SH.status(), "log": AU.log[-300:], "auto": [{"on": AU.run[i], **{k: v for k, v in AU.info[i].items() if k in ("msg", "steps", "clears", "quest")}, "diff": DIFFS[min(AU.info[i].get("diff", 0), len(DIFFS) - 1)], "done": AU.info[i].get("diff", 0) >= len(DIFFS)} for i in range(len(adbs))]})
                if self.path.startswith("/shuka/limg"):
                    from urllib.parse import urlparse, parse_qs
                    lid = "".join(ch for ch in (parse_qs(urlparse(self.path).query).get("id") or [""])[0] if ch.isdigit())
                    fp = os.path.join(SHUKA_IMG, lid + ".png")
                    return self.reply(200, open(fp, "rb").read() if os.path.exists(fp) else b"", "image/png")
                if self.path.startswith("/shuka/thumb"):
                    from urllib.parse import urlparse, parse_qs
                    i = int((parse_qs(urlparse(self.path).query).get("i") or ["0"])[0])
                    return self.reply(200, SH.inst[i]["png"] or png_small(b"\0" * 64, 0, 4, 4, 1), "image/png")
                if self.path.startswith("/watch"):
                    from urllib.parse import urlparse, parse_qs
                    qs = parse_qs(urlparse(self.path).query)
                    return self.reply(200, W.wait(int((qs.get("since") or ["-1"])[0])))
                if self.path.startswith("/screen"):
                    from urllib.parse import urlparse, parse_qs
                    i = int((parse_qs(urlparse(self.path).query).get("i") or ["0"])[0])
                    A = adbs[i] if 0 <= i < len(adbs) else adb
                    if not A.size:
                        A.connect()
                    try:
                        return self.reply(200, A.screen_bmp(), "image/bmp")
                    except Exception as e:
                        print("bmp failed, png:", e)
                        return self.reply(200, A.screencap(), "image/png")
                return self.reply(404, {"ok": False, "error": "not found"})
            except Exception as e:
                return self.reply(500, {"ok": False, "error": str(e)})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                q = json.loads(self.rfile.read(n) or b"{}")
                if self.path.startswith("/shoot"):
                    # every connected instance at the same time (BlueStacks' own sync only copies real mouse input)
                    import threading
                    seed = random.random(); res = [None] * len(adbs)
                    W.busy = time.time() + 30
                    ss = q.get("ss")    # [x, y]: tap the turn's portrait first (strike shot), then pull
                    def one(i, A):
                        try:
                            if not A.size:
                                A.connect()
                            A.rot_override = q.get("rot")
                            w, h = A.size
                            g = q.get("g")   # guardian beast first (its attack plays), then the strike shot, then the pull
                            if g:
                                A.tap(float(g[0]), float(g[1]), seed)
                                time.sleep(random.Random(seed).uniform(.2, .3))   # like the strike shot: just armed, no waiting
                            if ss:
                                A.tap(float(ss[0]), float(ss[1]), seed)
                                time.sleep(random.Random(seed).uniform(.2, .3))
                            res[i] = A.shoot(float(q["angle"]), q.get("cx", w / 2), q.get("cy", h * .45), float(q.get("ms", 1200)),
                                             float(q.get("hold", 400)), bool(q.get("wander")), q.get("method", "auto"), seed)
                        except Exception as e:
                            res[i] = {"error": f"{A.serial}: {e}"}
                    th = [threading.Thread(target=one, args=(i, A)) for i, A in enumerate(adbs)]
                    for t in th: t.start()
                    for t in th: t.join()
                    W.busy = time.time() + 1.5
                    print(f"shoot {q['angle']:.2f}deg  " + " | ".join(f"{A.serial}: {r.get('how') if r and 'error' not in r else r}" for A, r in zip(adbs, res)))
                    r0 = res[0] or {}
                    if "error" in r0:
                        raise RuntimeError(r0["error"])
                    errs = [r["error"] for r in res[1:] if r and "error" in r]
                    return self.reply(200, {"ok": True, **r0, "synced": len(adbs) - len(errs), "notes": r0.get("notes", []) + errs})
                if self.path.startswith("/shuka/run"):
                    SH.on = bool(q.get("on")); print("書庫: 画面の判定を" + ("始めました" if SH.on else "止めました"))
                    return self.reply(200, SH.status())
                if self.path.startswith("/shuka/auto"):
                    i = int(q.get("i", 0)); AU.run[i] = bool(q.get("on")); SH.on = SH.on or AU.run[i]
                    if AU.run[i]: AU.info[i].update(ai_n=0, diff=0, filter_for=None, filter_try={}, h_last=None, same=0, deck=None, deck_for=None)     # every start: from 極 again
                    AU.info[i]["msg"] = "動いています" if AU.run[i] else "停止中"
                    print(f"書庫自動 {adbs[i].serial}: " + ("開始" if AU.run[i] else "停止"))
                    return self.reply(200, {"ok": True})
                if self.path.startswith("/shuka/relabel"):
                    return self.reply(200, {"ok": SH.relabel(str(q["id"]), q["state"])})
                if self.path.startswith("/shuka/fix"):
                    return self.reply(200, {"ok": SH.fix(int(q["i"]), q["state"])})
                if self.path.startswith("/shuka/key"):
                    k = str(q.get("key", "")).strip()
                    if not k.startswith("sk-ant-"):
                        return self.reply(400, {"ok": False, "error": "sk-ant- で始まるキーを入れてください"})
                    with open(KEY_FILE, "w") as f: f.write(k)
                    return self.reply(200, {"ok": True})
                if self.path.startswith("/update"):   # the tool asks: get the newest helper and restart
                    import threading
                    def go():
                        if self_update():
                            time.sleep(.5); restart_for_update()
                    ok = True
                    threading.Thread(target=go, daemon=True).start()
                    return self.reply(200, {"ok": ok, "ver": HELPER_VER})
                if self.path.startswith("/tap"):    # e.g. the guardian beast: the same tap on every instance
                    import threading
                    seed = random.random(); out = [None] * len(adbs); W.busy = time.time() + 5
                    def one(i, A):
                        try:
                            if not A.size: A.connect()
                            A.rot_override = q.get("rot"); out[i] = A.tap(float(q["x"]), float(q["y"]), seed)
                        except Exception as e:
                            out[i] = f"error {A.serial}: {e}"
                    th = [threading.Thread(target=one, args=(i, A)) for i, A in enumerate(adbs)]
                    for t in th: t.start()
                    for t in th: t.join()
                    W.busy = time.time() + .8
                    print(f"tap ({q['x']:.0f},{q['y']:.0f}) {q.get('what', '')}: " + " | ".join(map(str, out)))
                    if str(out[0]).startswith("error"):
                        raise RuntimeError(out[0])
                    return self.reply(200, {"ok": True, "synced": sum(1 for o in out if not str(o).startswith("error"))})
                return self.reply(404, {"ok": False, "error": "not found"})
            except Exception as e:
                return self.reply(500, {"ok": False, "error": str(e)})

        def log_message(self, *a):
            pass

    return H


UPDATE_URL = "https://square-brook-4757.naraonara123.workers.dev/helper/monst_helper.py"
HERE = os.path.dirname(os.path.abspath(__file__))
RESTART_FLAG = os.path.join(HERE, "restart.flag")

# ======================= 書庫周回（段階1：画面の判定だけ。まだ何も押さない） =======================
SHUKA_STATES = {
    "home": "ホーム（真ん中に大きな「クエスト」ボタン）",
    "quest_list": "クエスト一覧（すべて／おすすめ…のタブ、クエストのアイコンが並ぶ）",
    "shuka_list": "追憶の書庫の一覧（左上に「追憶の書庫」、モンスターのアイコンの格子）",
    "filter": "書庫の絞り込み・並び替えの窓（「絞り込み」「決定」）",
    "quest_detail": "クエストの難易度を選ぶ画面（究極・極などの帯と「詳細」）",
    "play_type": "プレイタイプ選択（青い「ソロ」とオレンジの「マルチ募集」の大きなボタン）",
    "helper_select": "助っ人選択",
    "deck_select": "デッキ選択（「出撃」ボタン）",
    "loading": "通信中・真っ黒・クエスト名だけの読み込み画面",
    "battle": "バトル中",
    "result": "リザルト（スペシャル報酬、クリアタイム、OK など）",
    "lose": "負け・コンティニューの画面",
    "dialog": "その他の確認ウィンドウ・お知らせ",
    "other": "上のどれでもない画面",
}
SHUKA_FILE = os.path.join(HERE, "shuka_learn.json")
SHUKA_LOG = os.path.join(HERE, "shuka_clears.json")
SHUKA_IMG = os.path.join(HERE, "shuka_img")
SHUKA_MODEL = "claude-haiku-5-5"
KEY_FILE = None   # set in main()


def _gray_grid(data, off, w, h, y0f, y1f, gw, gh):
    out = []
    for gy in range(gh):
        ya = int(h * (y0f + (y1f - y0f) * gy / gh)); yb = int(h * (y0f + (y1f - y0f) * (gy + 1) / gh))
        for gx in range(gw):
            xa = int(w * gx / gw); xb = int(w * (gx + 1) / gw)
            s = n = 0
            for y in range(ya, max(ya + 1, yb), max(1, (yb - ya) // 4)):
                o = off + (y * w) * 4
                for x in range(xa, max(xa + 1, xb), max(1, (xb - xa) // 4)):
                    i = o + x * 4
                    s += data[i] * 30 + data[i + 1] * 59 + data[i + 2] * 11; n += 1
            out.append(s / n / 100)
    return out


def screen_sig(data, off, w, h):
    """what identifies a screen: the title row under the profile bar + the bottom menu (24x6 + 24x3 grey cells)"""
    return _gray_grid(data, off, w, h, .12, .24, 24, 6) + _gray_grid(data, off, w, h, .92, 1.0, 24, 3)


def is_shuka_list(data, off, w, h):
    if _search_bar(data, off, w, h): return True
    # without the search bar: the 書庫 header and no difficulty bands - and never a battle screen (party panel)
    return shuka_header(data, off, w, h) and not Shuka._panel(None, data, off, w, h) and not quest_bands(data, off, w, h) \
        and turn_from_rgba(data, off, w, h) is None


def _search_bar(data, off, w, h):
    """the 書庫 list itself (not the quest page under the same title): the light 「モンスター名で検索」 bar is there"""
    best = 0
    for yf in (.365, .375, .385, .395, .405):
        o = off + int(h * yf) * w * 4; n = t = 0
        for x in range(int(w * .1), int(w * .7), 8):
            i = o + x * 4; t += 1
            if min(data[i], data[i + 1], data[i + 2]) > 195: n += 1
        best = max(best, n / t)
    return best > .8


def full_sig(data, off, w, h):
    """coarse look of the whole middle of the screen (tells apart screens that share the title band and menu)"""
    return _gray_grid(data, off, w, h, .2, .9, 8, 14)


def _frac(data, off, w, h, box, pred, step=4):
    x0, y0, x1, y1 = box; n = t = 0
    for y in range(int(h * y0), int(h * y1), step):
        o = off + y * w * 4
        for x in range(int(w * x0), int(w * x1), step):
            i = o + x * 4; t += 1
            if pred(data[i], data[i + 1], data[i + 2]): n += 1
    return n / max(1, t)


_BLUE = lambda r, g, b: b > 180 and r < 120 and g > 90
_ORANGE = lambda r, g, b: r > 200 and 100 < g < 210 and b < 90
_TEAL = lambda r, g, b: g > 70 and b > 80 and r < g * .6 and abs(g - b) < 70
_YELLOW = lambda r, g, b: r > 200 and g > 160 and b < 90


def is_home(data, off, w, h):
    """home: the blue ミッション and orange マルチ参加 buttons beside the big クエスト button"""
    return _frac(data, off, w, h, (.04, .83, .3, .87), _BLUE) > .4 and _frac(data, off, w, h, (.68, .83, .94, .87), _ORANGE) > .3


def is_play_type(data, off, w, h):
    """solo / multi choice: a big blue ソロ and a big orange マルチ募集"""
    return _frac(data, off, w, h, (.1, .4, .42, .6), _BLUE) > .35 and _frac(data, off, w, h, (.6, .4, .9, .6), _ORANGE) > .3


def is_deck_select(data, off, w, h):
    """deck select: the teal 出撃 button low in the middle (not the round クエスト button of home) and a deck name"""
    return (_frac(data, off, w, h, (.40, .80, .58, .85), _TEAL) > .4 and _frac(data, off, w, h, (.40, .86, .58, .89), _TEAL) < .2
            and not is_home(data, off, w, h) and sum(deck_bits(data, off, w, h)) > .3)


def is_loading(data, off, w, h):
    """black, or the grey patterned loading screen (dark and colourless)"""
    n = t = sat = 0
    for y in range(int(h * .08), h, max(1, h // 48)):
        o = off + y * w * 4
        for x in range(0, w, max(1, w // 27)):
            i = o + x * 4; r, g, b = data[i], data[i + 1], data[i + 2]
            t += 1; n += (r * 30 + g * 59 + b * 11) / 100; mx = max(r, g, b); sat += (mx - min(r, g, b)) * 255 / mx if mx else 0
    mean, sat = n / t, sat / t
    return mean < 8 or (mean < 45 and sat < 35)


def quest_bands(data, off, w, h):
    """the difficulty bands of a quest page: centre y (pixels) of each teal 詳細 button on the right, top first"""
    fr = []
    for y in range(int(h * .25), int(h * .86), 2):
        o = off + y * w * 4; n = t = 0
        for x in range(int(w * .80), int(w * .91), 3):
            i = o + x * 4; t += 1
            if _TEAL(data[i], data[i + 1], data[i + 2]): n += 1
        fr.append((y, n / t))
    runs = []
    for y, f in fr:
        if f > .5:
            if runs and y - runs[-1][1] <= h * .025: runs[-1][1] = y
            else: runs.append([y, y])
    return [(a + b) / 2 for a, b in runs if h * .045 <= b - a <= h * .085]


def has_clear_tag(data, off, w, h):
    """the yellow CLEAR! tag on the quest banner (that quest's reward is already taken)"""
    return _frac(data, off, w, h, (.04, .285, .26, .325), _YELLOW, 2) > .05


DECK_BOX = (.28, .43, .56, .47)      # deck name above the characters on the deck select screen


def deck_bits(data, off, w, h, box=None):
    x0, y0, x1, y1 = box or DECK_BOX; out = []
    for gy in range(4):
        ya = int(h * (y0 + (y1 - y0) * gy / 4)); yb = int(h * (y0 + (y1 - y0) * (gy + 1) / 4))
        for gx in range(24):
            xa = int(w * (x0 + (x1 - x0) * gx / 24)); xb = int(w * (x0 + (x1 - x0) * (gx + 1) / 24)); n = t = 0
            for y in range(ya, max(ya + 1, yb), 2):
                o = off + y * w * 4
                for x in range(xa, max(xa + 1, xb), 2):
                    i = o + x * 4; t += 1
                    if min(data[i], data[i + 1], data[i + 2]) > 170: n += 1
            out.append(n / max(1, t))
    return out


def orange_buttons(data, off, w, h):
    """orange buttons (OK / 諦める / 挑戦する ...) on screen: [(cx, cy)] in pixels, left to right then top to bottom"""
    st = 6; cells = set()
    for y in range(int(h * .25), int(h * .92), st):
        o = off + y * w * 4
        for x in range(0, w, st):
            i = o + x * 4; r, g, b = data[i], data[i + 1], data[i + 2]
            if r > 200 and 90 < g < 185 and b < 70: cells.add((x // st, y // st))
    seen = set(); out = []
    for c in cells:
        if c in seen: continue
        stack = [c]; seen.add(c); pts = []
        while stack:
            p = stack.pop(); pts.append(p)
            for d in ((1, 0), (-1, 0), (0, 1), (0, -1), (2, 0), (-2, 0), (0, 2), (0, -2)):
                q = (p[0] + d[0], p[1] + d[1])
                if q in cells and q not in seen: seen.add(q); stack.append(q)
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        bw, bh = (max(xs) - min(xs) + 1) * st, (max(ys) - min(ys) + 1) * st
        if w * .16 < bw < w * .6 and h * .022 < bh < h * .08 and len(pts) * st * st > bw * bh * .35:
            out.append(((min(xs) + max(xs) + 1) * st / 2, (min(ys) + max(ys) + 1) * st / 2))
    return sorted(out, key=lambda p: (round(p[1] / (h * .05)), p[0]))


def list_sig(data, off, w, h):
    """the middle of the screen (the scrolling list), to see whether a scroll moved anything"""
    return _gray_grid(data, off, w, h, .3, .9, 12, 16)


def sig_dist(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) / 255


def png_small(data, off, w, h, step=4):
    """the frame as a small PNG (no PIL needed)"""
    import zlib
    W, H = w // step, h // step
    raw = bytearray()
    for y in range(H):
        raw.append(0); o = off + (y * step * w) * 4
        for x in range(W):
            i = o + x * step * 4; raw += data[i:i + 3]
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


AI_USE = {"in": 0, "out": 0}
YEN_PER_USD = 155


def ai_yen():
    """real cost so far from the token counts the API reports (Haiku 5.5: $0.10 in / $0.50 out per 1M tokens)"""
    return (AI_USE["in"] * 0.10 + AI_USE["out"] * 0.50) / 1e6 * YEN_PER_USD


def _count(j):
    import inspect
    u = j.get("usage") or {}
    i_ = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0)
    o_ = int(u.get("output_tokens", 0))
    AI_USE["in"] += i_; AI_USE["out"] += o_
    what = {"ask_claude_action": "画面の判断", "ask_claude_deck": "デッキ（画面全体）", "ask_claude_deckname": "デッキ名",
            "ask_claude_head": "見出し", "ask_claude_state": "画面の判定", "ask_claude_count": "件数", "ask_claude_any_quest": "一覧の確認"}
    try: fn = inspect.stack()[1].function
    except Exception: fn = "?"
    print(f"　（AI {what.get(fn, fn)}：約{(i_ * 0.10 + o_ * 0.50) / 1e6 * YEN_PER_USD:.3f}円）")


SHUKA_GROUPS = os.path.join(HERE, "shuka_heads.json")      # learned headings (v79: a fresh start, older reads were unreliable)
SHUKA_POS = os.path.join(HERE, "shuka_pos.json")      # where 出撃 is and where to swipe the deck (fractions of the screen)


def png_crop(data, off, w, h, x0, y0, x1, y1, step=1):
    import zlib
    xa, xb, ya, yb = int(w * x0), int(w * x1), int(h * y0), int(h * y1)
    raw = bytearray(); W = len(range(xa, xb, step)); H = 0
    for y in range(ya, yb, step):
        raw.append(0); o = off + y * w * 4; H += 1
        for x in range(xa, xb, step):
            i = o + x * 4; raw += data[i:i + 3]
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


HEAD_BOX = (0, .40, .55, .50)      # where the list's group heading (黎絶 / 極 ...) sits, left side under the search box


def head_bits(data, off, w, h, yc=None):
    """white text of a heading band as a 28x8 grid of 'how much white' (the art behind it does not matter much).
    yc: centre of the band as a fraction of the height (default: the sticky heading under the search box)"""
    x0, y0, x1, y1 = HEAD_BOX
    if yc is not None: y0, y1 = yc - (y1 - y0) / 2, yc + (y1 - y0) / 2
    out = []
    for gy in range(8):
        ya = int(h * (y0 + (y1 - y0) * gy / 8)); yb = int(h * (y0 + (y1 - y0) * (gy + 1) / 8))
        for gx in range(28):
            xa = int(w * (x0 + (x1 - x0) * gx / 28)); xb = int(w * (x0 + (x1 - x0) * (gx + 1) / 28)); n = t = 0
            for y in range(ya, max(ya + 1, yb), 3):
                o = off + y * w * 4
                for x in range(xa, max(xa + 1, xb), 3):
                    i = o + x * 4; t += 1
                    if data[i] > 205 and data[i + 1] > 205 and data[i + 2] > 205: n += 1
            out.append(n / max(1, t))
    return out


def bits_dist(a, b):
    return sum(abs(p - q) for p, q in zip(a, b)) / len(a)


def norm_group(t):
    t = (t or "").translate(str.maketrans("０１２３４５６７８９☆", "0123456789★"))
    t = re.sub(r"[\s　・]", "", t).replace("星", "★")
    return re.sub(r"\d+$", "", t) if not t.endswith("制限") else t     # a count read by mistake at the end


def canon(name):
    """group name -> what we do with it: kiwami / star5 / kyuu / geki, or None (harder groups, multi-only)"""
    t = norm_group(name)
    if not t or "マルチ" in t: return None
    if "★" in t or "制限" in t: return "star5" if "究極" in t or t.startswith("★") else None
    if "激究極" in t: return "geki"
    if t == "究極": return "kyuu"
    if t == "極": return "kiwami"
    return None


GROUP_KEYS = ["kiwami", "star5", "kyuu", "geki"]
DIFFS = ["極", "★5以下制限", "究極", "激究極"]           # the 難易度 filter, in this order (same order as GROUP_KEYS)


def first_icon(data, off, w, h, start=.30):
    """(top, centre) y of the top-left quest icon of the 書庫 list, or None when the list is empty.
    Looks for the first block of colourful art in the left column that is as tall as an icon
    (a row cut off at the top of the list is shorter and skipped)."""
    fr = []
    for y in range(int(h * start), int(h * .7), 3):
        o = off + y * w * 4; n = t = 0
        for x in range(int(w * .05), int(w * .21), 4):
            i = o + x * 4; r, g, b = data[i], data[i + 1], data[i + 2]; mx = max(r, g, b); t += 1
            if mx > 90 and mx - min(r, g, b) > 70: n += 1
        fr.append((y, n / t))
    j = 0
    while j < len(fr):
        if fr[j][1] > .3:
            k = end = j; low = 0
            for m in range(j + 1, len(fr)):
                if fr[m][1] > .08: end = m; low = 0
                else:
                    low += 1
                    if low > 2: break
            y0, y1 = fr[k][0], fr[end][0]
            if w * .08 < y1 - y0 < w * .24: return y0, (y0 + y1) / 2
            j = end + 1
        else:
            j += 1
    return None


def first_icon_y(data, off, w, h):
    f = first_icon(data, off, w, h)
    return f[1] if f else None


def shuka_header(data, off, w, h):
    """the 書庫 header: the orange-rimmed 書庫pt / 排出率 bar under the title (on the list and on a quest page)"""
    o = lambda r, g, b: r > 200 and 90 < g < 190 and b < 80
    return _frac(data, off, w, h, (.47, .19, .95, .205), o) > .07 and _frac(data, off, w, h, (.47, .23, .95, .245), o) > .07          # the order to clear: 極 > 究極★5以下制限 > 究極 > 激究極


def ask_claude_head(png):
    import base64, urllib.request
    key = open(KEY_FILE).read().strip() if KEY_FILE and os.path.exists(KEY_FILE) else ""
    if not key: return None
    body = {"model": SHUKA_MODEL, "max_tokens": 200, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": "モンストの「追憶の書庫」一覧の一部です。一番上に見えているグループ見出しの文字（例：黎絶、轟絶、爆絶、超絶、超究極、超究極・封、究極、究極 ★5以下制限、極、極 マルチ専用、激究極）を、見出しの文字だけ1行で答えてください。右の数字はいりません。見出しが無ければ none。"}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read()); _count(j)
        t = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text").strip().splitlines()
        return t[0].strip() if t else None
    except Exception:
        return None


def ask_claude_deck(png, W, H):
    """deck select: which deck is shown, where 出撃 is, where the deck name band is. Asked only for a deck look not seen before."""
    import base64, urllib.request, re as _re
    key = open(KEY_FILE).read().strip() if KEY_FILE and os.path.exists(KEY_FILE) else ""
    if not key: return None
    txt = (f"モンストのデッキ選択画面です（幅{W}、高さ{H}のピクセル座標）。"
           "画面の上のほうにある大きな帯の文字はクエスト名です。デッキ名ではありません。"
           "デッキ名は、自分のキャラ（モンスター）が横に並んだ枠のすぐ上か枠の上の端に、小さめの文字で書いてあります（例：「書庫周回」「書庫★5制限」）。"
           "まずクエスト名を quest に、次にデッキ名を name に書き、デッキ名が「書庫周回」なら deck を normal、「書庫★5制限」（☆や５でも同じ）なら star5、それ以外は other。"
           '出撃ボタンの中心と、デッキ名の文字の中心の y も答えてください。JSONだけで: {"quest":"クエスト名","name":"デッキ名","deck":"normal","go":[x,y],"name_y":y}')
    body = {"model": SHUKA_MODEL, "max_tokens": 600, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": txt}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read()); _count(j)
        t = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
        for m in reversed(list(_re.finditer(r"\{[^{}]*\}", t))):       # the last flat {...} in the answer
            try: return json.loads(m.group(0))
            except Exception: continue
        d = {}
        for k in ("quest", "name", "deck"):                              # or pick the fields one by one
            m = _re.search(k + r'"?\s*[:：]\s*"([^"]*)"', t)
            if m: d[k] = m.group(1)
        m = _re.search(r'go"?\s*[:：]\s*\[\s*([\d.]+)\s*,\s*([\d.]+)', t); n = _re.search(r'name_y"?\s*[:：]\s*([\d.]+)', t)
        if m and n and d.get("name"):
            d["go"] = [float(m.group(1)), float(m.group(2))]; d["name_y"] = float(n.group(1)); return d
        print("（デッキの読み取り：AIの返事）", t[:120].replace("\n", " "))
        return None
    except Exception:
        return None


def ask_claude_deckname(png):
    """the small crop of the deck name: normal (書庫周回) / star5 (書庫★5制限) / other"""
    import base64, urllib.request
    key = open(KEY_FILE).read().strip() if KEY_FILE and os.path.exists(KEY_FILE) else ""
    if not key: return None, ""
    body = {"model": SHUKA_MODEL, "max_tokens": 100, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": "モンストのデッキ名の部分です。書いてある文字だけを1行で答えてください。"}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read()); _count(j)
        t = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text").strip().splitlines()[0]
    except Exception:
        return None, ""
    n = norm_group(t)
    if "書庫" in n and ("★" in n or "制限" in n): return "star5", t
    if "書庫" in n and "周回" in n: return "normal", t
    return "other", t


def ask_claude_count(png):
    """the number on the right of the 書庫 list heading (how many quests in the group); None if not readable"""
    import base64, urllib.request
    key = open(KEY_FILE).read().strip() if KEY_FILE and os.path.exists(KEY_FILE) else ""
    if not key: return None
    body = {"model": SHUKA_MODEL, "max_tokens": 30, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": "モンストの書庫一覧のグループ見出しの帯です（左に難易度名、右に件数）。右端の件数の数字だけを答えてください。見出しが無ければ none。"}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read()); _count(j)
        t = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
        m = re.search(r"\d+", t.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        return int(m.group(0)) if m else None
    except Exception:
        return None


def ask_claude_any_quest(png, diff):
    """is any quest left on this filtered 書庫 list? True / False, None when unsure or no answer"""
    import base64, urllib.request
    key = open(KEY_FILE).read().strip() if KEY_FILE and os.path.exists(KEY_FILE) else ""
    if not key: return None
    body = {"model": SHUKA_MODEL, "max_tokens": 50, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": f"モンストの「追憶の書庫」の一覧です（難易度「{diff}」で絞り込み中のはず）。一覧にクエスト（モンスターのアイコン）が1つでも表示されていれば yes、1つもなければ no、一覧の画面でない・読み込み中なら unsure とだけ答えてください。"}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read()); _count(j)
        t = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text").strip().lower()
    except Exception:
        return None
    return True if t.startswith("yes") else False if t.startswith("no") else None


def ask_claude_state(png):
    """unknown screen -> ask Claude (Haiku) which screen it is. Returns (state, note, raw text)"""
    import base64, urllib.request, urllib.error
    key = open(KEY_FILE).read().strip() if KEY_FILE and os.path.exists(KEY_FILE) else ""
    if not key:
        return None, "APIキーが未設定", ""
    states = "\n".join(f"{k} : {v}" for k, v in SHUKA_STATES.items())
    body = {"model": SHUKA_MODEL, "max_tokens": 1500, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": "これはスマホゲーム「モンスターストライク」の画面です。下の一覧から一番近い画面を1つ選んでください。\n"
                                 "1行目に左側の英語の名前だけ、2行目に20文字以内で画面の説明を書いてください。ほかには何も書かないでください。\n\n" + states}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read()); _count(j)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="ignore")[:300]
        print("書庫: AIへの問い合わせに失敗:", e.code, msg)
        return None, f"AIエラー {e.code}: {msg[:60]}", msg
    txt = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text").strip()
    if not txt:   # nothing written (ran out of tokens, refused...): do not learn anything from it
        print("書庫: AIの答えが空でした:", j.get("stop_reason"), [c.get("type") for c in j.get("content", [])])
        return None, f"AIの答えが空（{j.get('stop_reason')}）", 
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    st = None
    for l in lines[:2]:
        for k in SHUKA_STATES:
            if l.lower().strip("`*:：- ").startswith(k):
                st = k; break
        if st: break
    if not st:
        hits = [k for k in SHUKA_STATES if k in txt]
        st = hits[0] if hits else "other"
    note = next((l for l in lines if not any(k in l.lower() for k in SHUKA_STATES) and not l.startswith("`")), "")[:40]
    if st == "other":
        print("書庫: AIの答え（other）:", txt[:200].replace("\n", " / "))
    return st, note, txt


class Shuka:
    """polls every instance, tells which screen it is on: known look -> instantly, new look -> asks Claude once and remembers it"""
    def __init__(self, adbs):
        import threading
        self.adbs, self.on, self.lock = adbs, False, threading.Lock()
        try: self.learn = json.load(open(SHUKA_FILE, encoding="utf-8"))
        except Exception: self.learn = []          # [{id, state, note, sig, n}]
        self.learn = [L for L in self.learn if not (L.get("state") == "other" and not L.get("note"))]   # v50-53: unreadable answers
        for L in self.learn: L.setdefault("id", f"{int(time.time() * 1000)}{random.randint(100, 999)}")
        os.makedirs(SHUKA_IMG, exist_ok=True)
        self.inst = [{"serial": A.serial, "state": "-", "note": "", "how": "", "png": b"", "sig": None, "t": 0} for A in adbs]
        self.ai_calls = 0
        for i, A in enumerate(adbs):
            threading.Thread(target=self.loop, args=(i, A), daemon=True).start()

    def save(self):
        try:
            with open(SHUKA_FILE, "w", encoding="utf-8") as f: json.dump(self.learn, f, ensure_ascii=False)
        except Exception as e: print("書庫: 保存できません", e)

    def classify(self, data, off, w, h):
        if turn_from_rgba(data, off, w, h) or self._panel(data, off, w, h):
            return "battle", "パーティ欄あり", "見た目", None
        avg = sum(_gray_grid(data, off, w, h, 0, 1, 6, 10)) / 60
        if avg < 18:
            return "loading", "真っ暗", "見た目", None
        sig = screen_sig(data, off, w, h)
        best = min(self.learn, key=lambda L: sig_dist(sig, L["sig"]), default=None)
        if best and sig_dist(sig, best["sig"]) < .06:
            best["n"] = best.get("n", 0) + 1
            return best["state"], best.get("note", ""), "見本", sig
        return None, "", "", sig

    def _panel(self, data, off, w, h):
        y = int(h * .8); o = off + y * w * 4; n = t = 0
        for x in range(0, w, 6):
            i = o + x * 4; t += 1
            if data[i] > 200 and 140 < data[i + 1] < 220 and data[i + 2] < 80: n += 1
        return n > t * .5

    def loop(self, i, A):
        st = self.inst[i]; prev_sig = None; waits = 0
        while True:
            time.sleep(random.uniform(2.5, 4.0))
            if not self.on or (getattr(self, "auto", None) and self.auto.run[i]):
                continue
            try:
                if not A.size: A.connect()
                data, off, w, h = A.raw_frame()
                state, note, how, sig = self.classify(data, off, w, h)
                st["png"] = png_small(data, off, w, h); st["t"] = time.time(); st["sig"] = sig
                if state is None:
                    # wait a little for the screen to hold still (an animated screen like home never does: ask anyway after 2 tries)
                    if (prev_sig is None or sig_dist(sig, prev_sig) > .05) and waits < 2:
                        prev_sig = sig; waits += 1; st.update(state="?", note="判定中（画面が落ち着くのを待っています）", how=""); continue
                    waits = 0
                    state, note, raw = ask_claude_state(png_small(data, off, w, h, 2))
                    if state is None:
                        st.update(state="?", note=note, how=""); time.sleep(10); continue
                    with self.lock:
                        self.ai_calls += 1
                        self.add(state, note, sig, png_small(data, off, w, h, 6))
                    how = "AI（覚えました）"
                    print(f"書庫 {A.serial}: 新しい画面 → {state}（{note}）")
                prev_sig = sig
                st.update(state=state, note=note, how=how)
            except Exception as e:
                st.update(state="×", note=str(e)[:80], how="")

    def add(self, state, note, sig, thumb):
        lid = f"{int(time.time() * 1000)}{random.randint(100, 999)}"
        try:
            with open(os.path.join(SHUKA_IMG, lid + ".png"), "wb") as f: f.write(thumb)
        except Exception: pass
        self.learn.append({"id": lid, "state": state, "note": note, "sig": [round(v, 1) for v in sig], "n": 1}); self.save()

    def relabel(self, lid, state):
        with self.lock:
            if state == "_delete":
                self.learn = [L for L in self.learn if L["id"] != lid]
            else:
                for L in self.learn:
                    if L["id"] == lid and state in SHUKA_STATES: L["state"] = state; L["note"] = "手で直した"
            self.save()
        return True

    def fix(self, i, state):
        """the user corrects the last screen of instance i"""
        sig = self.inst[i].get("sig")
        if not sig or state not in SHUKA_STATES: return False
        with self.lock:
            for L in self.learn:
                if sig_dist(sig, L["sig"]) < .06: L["state"] = state; L["note"] = "手で直した"
            if not any(sig_dist(sig, L["sig"]) < .06 for L in self.learn):
                self.add(state, "手で直した", sig, self.inst[i].get("png") or b"")
            self.save()
        self.inst[i].update(state=state, note="手で直した", how="見本")
        return True

    def status(self):
        return {"on": self.on, "ai_calls": self.ai_calls, "yen": round(ai_yen(), 3), "tok": dict(AI_USE), "learned": len(self.learn), "key": bool(KEY_FILE and os.path.exists(KEY_FILE)),
                "states": SHUKA_STATES, "learn": [{k: L.get(k) for k in ("id", "state", "note", "n")} for L in self.learn], "inst": [{k: v for k, v in s.items() if k in ("serial", "state", "note", "how", "t")} for s in self.inst]}




# ======================= 書庫周回（段階2：自動で回す。画面ごとの操作はAIが決め、決まった操作は覚える） =======================
SHUKA_GOAL = """モンストの自動周回（追憶の書庫のクエストをソロで1つずつクリア）を見守っています。いつもの画面はプログラムが操作します。あなたが呼ばれるのは知らない画面のときだけです。
- 絞り込みの窓（filter）：「絞り込み」タブで、難易度を流れで指定されたもの（極・★5以下制限・究極・激究極）に、初クリア報酬を「報酬未受取」にして、緑の「決定」（why に「決定」）。選択肢の一覧では指定のものを1つ押す。
- クエスト一覧では「追憶の書庫」。助っ人選択では「現在のフレンド使用キャラ」の一番上（fixed true）。書庫の一覧では wait。右上の丸い「一覧」は押さない。
- デッキ名が流れの指定と違えば横スワイプ（書庫周回の右隣が書庫★5制限。右隣へは指を右→左）。
- リザルト・スペシャル報酬・獲得・ランクアップ・ログインボーナス・日付変更などは画面を押すか OK／閉じる／次へ。お知らせは閉じる・×。通信エラーはリトライ。タイトル画面は画面を押す。
- EXTRA STAGE は絶対に挑戦しない（「諦める」「あきらめる」を押す）。負け・コンティニュー確認では「あきらめる」。
- 書庫ポイント不足・ボックスいっぱい・スタミナ不足など遊べない表示は stop（why に理由）。オーブ・購入・コンティニューの画面、どうしても分からない画面も stop。
- 読み込み・演出中は wait。「通信中」の表示があっても奥にボタンの窓があれば押してよい。先に進む操作がはっきりしていればそれを選び、戻るのは書庫と関係ない所に来たときだけ。
"""
SHUKA_ACT_FMT = """次の操作を1つ決めて、JSONだけで答えてください（説明文は不要）。座標はこの画像のピクセル（左上が0,0、幅{W}、高さ{H}）。
{{"state":"<画面の名前: {states}>","action":"tap|longpress|swipe|wait|back|stop","x":0,"y":0,"x2":0,"y2":0,"why":"20文字以内","fixed":true,"quest":""}}
- quest は画面にクエスト名が見えていればその名前（見えなければ空）。
- tap / longpress は押す点の中心。swipe は (x,y) から (x2,y2) へ。
- fixed は「この画面ならいつも同じ場所を押せばいい」なら true、そのときの中身（どのクエストか等）で押す場所が変わるなら false。
これまでの流れ（古い順）: {hist}
思っていた画面と違うとき（押したのに進んでいない、知らない画面が出た）は、流れから今どこにいるかを考えて軌道修正してください。"""


def ask_claude_action(png, W, H, hist):
    import base64, urllib.request, urllib.error, re
    key = open(KEY_FILE).read().strip() if KEY_FILE and os.path.exists(KEY_FILE) else ""
    if not key:
        return None, "APIキーが未設定"
    text = SHUKA_GOAL + "\n" + SHUKA_ACT_FMT.format(W=W, H=H, states="|".join(SHUKA_STATES), hist=" / ".join(hist[-14:]) or "なし")
    body = {"model": SHUKA_MODEL, "max_tokens": 1500, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": text}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.loads(r.read()); _count(j)
    except urllib.error.HTTPError as e:
        return None, f"AIエラー {e.code}: {e.read().decode(errors='ignore')[:80]}"
    txt = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None, "AIの答えを読めません: " + txt[:60]
    try:
        a = json.loads(m.group(0))
    except Exception:
        return None, "AIの答えを読めません: " + txt[:60]
    if a.get("action") not in ("tap", "longpress", "swipe", "wait", "back", "stop"):
        return None, "知らない操作: " + str(a.get("action"))
    return a, ""


def ask_claude_battle(png, W, H, who):
    """our turn: where is the character labelled `who` (1st..4th) and the nearest other character"""
    import base64, urllib.request, urllib.error, re
    key = open(KEY_FILE).read().strip() if KEY_FILE and os.path.exists(KEY_FILE) else ""
    text = (f"モンストのバトル画面です。フィールド上で「{who}」と書かれた札の付いた味方キャラ（丸いアイコン）の中心と、"
            f"それ以外で一番近い味方キャラ（1st/2nd/3rd/4thの札が付いている）の中心を答えてください。座標はこの画像のピクセル（幅{W}、高さ{H}）。"
            'JSONだけで: {"me":[x,y],"ally":[x,y]}')
    body = {"model": SHUKA_MODEL, "max_tokens": 1500, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": text}]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.loads(r.read()); _count(j)
        txt = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
        a = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        return [float(v) for v in a["me"]], [float(v) for v in a["ally"]]
    except Exception as e:
        return None, None


# ---- 1st..4th labels above the balls (port of the tool's findLabelBalls, simplified, pure Python) ----
_TPL = {k: [[(int(c) + .5) / 10 for c in t] for t in v] for k, v in {'LABEL_TPL': ['000012683000000000000000000000000000024899995000000000000000000001100000468999995000000000000000000025205200002269995000000111000000000355256200000029995000025555532200003555998400000029995000356878999971035559998555000029993000579652279985523599755555000029992000199951000555510579755421000029992000027997420012100556655200000029992000003589875200000555555200000029992000000236998640000555555200000028692000110001465655000203555000000012062000010200020125210100566002000002032000000000000000010000202001000002010000000000000100001000101010000002000000000000100100000000000100', '001899990000000000000000000000016995049999999200000000000000000000006995397523899800000000000000000000006995830000399910000000000000000000006995000000099910111000133100000000026995000000299900588726899800000014689994000000799500599989999950000369859993000002998000499975399950004796206993000007993000499700049950026963003993000049950000499500039950069930002993000289600000499500039950069810002893000886100000189300039950069800002793004752000000055200038640055700002373032620022210010100010220010500002013010210000310000000010000010020100003000312200200000000000000000001102001', '000599996000000000000000000000006776029999999930000000000000000000009998398500599980000000000000000000009997410000059982000000000000000000005997000000069980000111000120000000009997000000299920002999317980000005799997000003997200002999999980001589759997000269995000002999842330039994005997000577998520002999500000189940005997000000289971001999200000699900005997000000028997000999000000799300005896000000007679100999000001789000000795000000013257000339000001757000000555000000110033000105000000201500003202000011000010000001000000000111010001001000100000000000000000000000000000', '001001776500000000000000477500000000000006999100000000021000599900000000000018996000000000571000599900000001000059980000000004971000599900000000000199996531000039982210599900013300000599959971000399999920499902699982000999249971002899999930499978999997003995029971001289993200499995249999008990029971000079970000499920007999029930029971000079970000499900004999059700027971000059950000499900002999099301156896620059920000388700000995569855555577620027710000266700000655625355320002420030200000201300000105000000101000000002010210200000000002000000000000000000001100000000000002'], 'LABEL_TPL_Y': ['005600000000000000000000000200000000005400000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000286666663000000000000000000003600900002589995000000000000000000029900000000009995000001333300000000199911000000009995000058999998640005999999000000009995000299700699995039999999000000009995000599600005881001699910000000009995000079992000030000599500000000009995000005999830000000599500000000009990000000159999610000199500000000009990000000000899982000099500000000009680000000000166678000097300000000005010000012000000003000050100', '000599851000000000000000000000000555017999998000000000000000000000000999089999999500000000000000000000000999883301599800000000000000000000000999200000399900000000000000000000000999000000399900255500055500000000005999000000399600599935999950000001589999000000699400599999999980000169963999000002998000099981039990000899500999000009995000099910009990005996000799000059970000099900009990009991000599000299600000099600009990019960000599000897000000089500009980059950000599005980000000069500009960009950000599019910000000035500005560006620000568021400030100020300005040000030001303', '001999996000000000000000000000009999029999999900000000000000000000009999299555799950000000000000000000009998550000099990000000000000000000006996000000059990000000000000000000006996000000099960001665102550000000139996000000799700003999759990000028999996000006998000003999999990001999528996000079995000003999950550008995005996001599998200003999500000079970003996000002699981001999500000599900003996000000029995000999000000999200003996000000009999000999000003999000003996000000009998000999000003999000003993000000025995000998000001999000003573000001110051000302000000502300003001', '000000996200000000000000155500000000000005999200000000000000699900000000000008998000000000085000699900000000000039994000000000591000699900000000000079996000000007990000699900000000000299999993000059997510399900055551000599959993000299999960399902699998001998108993003999999950399999999999005996006993000039980000399992002999006990006993000019960000399900000999029950006993000009960000399900000999069800006993000009960000299900000799599000009999910009960000099600000699599756999999930009960000099600000698999999996559910007530000077500000656133333333014300003020000030000000101'], 'LABEL_TPL_G': ['000000259500000000000000000000000000000009999500000000000000000000000000005599999500000000000000000000002200799999999500000000000000000000029500099999999500000000000000000000099500000559999500000000000000000000799500000009999500000000000000000005999500000009999500000599999550000029999999000009999500007999999999700299999999000009999500009999999999950599999999000009999500059992027999920999999997000005250500027527000292700007002500000000000000005000500009500005000000000000000000002200222000000002000000000000000000000500000500000000000000000000000000000002000000000000000000', '000599995000000000000000000000005998003999999300000000000000000000005999008999999800000000000000000000005999059999999950000000000000000000005999199755799970000000000000000000005999693000099990000000000000000000005999820000069990000000000000000000007999000000069990399951599950000000269999000000099950399966999990000006999999000000299920399999999993000069999999000000599900399999999993000599935999000001755500371596587053001758502508000003000300350060035003003003000306000003005000300030015003001011000103000000000000300030000003050030000000000000000000300010000003000000000000', '000259997000000000000000000000005555005999999900000000000000000000009999019999999970000000000000000000009999089995999992000000000000000000009999599000099995000000000000000000009999950000059999000000000000000000009999000000059995000000000000000000009999000000059995002799702791000000079999000000599970002999959993000057999999000000999900000999999993000999999999000008999200000999999993005999609999000273007000000500730051075577005209000990000900000500500000050090005005000025500022000500200000200020005005000000050005000500000000500500000005000000000000000500000000000200000000', '000079992000000000000001999700000000000099995000000000000003999900000000000599990000000000750003999900000000000599960000000003950003999900000000000999950000000008950003999900000000003999930000000059950003999900000000005999965500000299950003999900000000006999999900000999999903999900599972009999999900005999999903999909999995029992599900029999999903999999999999059990599900099999999903999999999999095720267700005775952203755795279559050500030500000500900000500500009005500500010500000500500000200200005005503000000500000500500000000000000005001000000500000200200000000000000002']}.items()}


def _ncc(a, b):
    n = len(a); ma = sum(a) / n; mb = sum(b) / n; ab = aa = bb = 0.0
    for x, y in zip(a, b):
        x -= ma; y -= mb; ab += x * y; aa += x * x; bb += y * y
    return ab / math.sqrt(aa * bb + 1e-9)


def _lcol(r, g, b):
    if r > 150 and g < 60 and b < 70 and r - g > 90: return 1                       # red
    if r > 150 and 95 < g < 215 and b < 70 and r - b > 120: return 2                # gold
    if b > 125 and b - r > 90 and g < 160 and b - g > 50: return 3                  # blue
    if g > 170 and r < 120 and b < 120 and g - r > 100 and g - b > 90: return 4     # green
    return 0


def find_labels(data, off, w, h):
    """[(n, x, y)] ball centres under the 1st/2nd/3rd/4th labels on the field (n = 0: label seen but not read)"""
    u = w / 1170; st = 2
    ya0, ya1 = int(h * .06), int(h * .82)
    cells = {}; white = set()
    for y in range(ya0, ya1, st):
        o = off + y * w * 4
        for x in range(0, w, st):
            i = o + x * 4; r, g, b = data[i], data[i + 1], data[i + 2]
            c = _lcol(r, g, b)
            if c: cells[(x // st, y // st)] = c
            elif r > 150 and g > 150 and b > 150: white.add((x // st, y // st))
    # the label text has a white outline: keep coloured cells with enough white around (not hair, effects...)
    rc = max(1, round(6 * u / st)); area = (2 * rc + 1) ** 2
    core = {}
    for p, c in cells.items():
        if c == 4: core[p] = c; continue          # green labels have a dark outline
        n = 0
        for dx in range(-rc, rc + 1):
            for dy in range(-rc, rc + 1):
                if (p[0] + dx, p[1] + dy) in white: n += 1
        if n / area > .12: core[p] = c
    hx, hy = max(1, round(13 * u / st)), max(1, round(4 * u / st))
    seen = set(); out = []; cands = []

    def mask_at(xx, yy, c):
        i = off + (yy * w + xx) * 4
        return 1 if _lcol(data[i], data[i + 1], data[i + 2]) == c else 0

    def feats(xa, ya, bw, bh, c, M=None):
        F = []
        for ty in range(16):
            ys = ya + ty * bh // 16; ye = max(ys + 1, ya + (ty + 1) * bh // 16)
            for tx in range(36):
                xs = xa + tx * bw // 36; xe = max(xs + 1, xa + (tx + 1) * bw // 36)
                if M:
                    I, X0, Y0, W1 = M
                    v = I[(ye - Y0) * W1 + xe - X0] - I[(ys - Y0) * W1 + xe - X0] - I[(ye - Y0) * W1 + xs - X0] + I[(ys - Y0) * W1 + xs - X0]
                    F.append(v / ((xe - xs) * (ye - ys)))
                else:
                    v = n = 0
                    for yy in range(ys, ye):
                        for xx in range(xs, xe): v += mask_at(xx, yy, c); n += 1
                    F.append(v / n)
        return F

    blob_sc = {}

    def read(F, c):
        best = (0, 0, 0, 58.5)
        for key, dy in (("LABEL_TPL_Y", 58.5),) if c == 2 else ((("LABEL_TPL_G", 66), ("LABEL_TPL", 58.5)) if c == 4 else (("LABEL_TPL", 58.5),)):
            sc = sorted(((_ncc(F, t), k + 1) for k, t in enumerate(_TPL[key])), reverse=True)
            for v, nn in sc:
                if v > blob_sc.get(nn, (-1,))[0]: blob_sc[nn] = (v, dy)
            if sc[0][0] > best[0]: best = (sc[0][0], sc[0][0] - sc[1][0], sc[0][1], dy)
        return best

    def slide(xa, xb, ya, yb, c):
        """label glued to something: slide a label-sized window around the blob"""
        X0, Y0 = max(0, xa - 6), max(ya0, ya - 6); X1, Y1 = min(w, xb + 7), min(ya1, yb + 7); W1 = X1 - X0 + 1
        I = [0] * (W1 * (Y1 - Y0 + 1))
        for yy in range(Y0, Y1):
            row = 0; base = (yy - Y0 + 1) * W1; prev = (yy - Y0) * W1
            for xx in range(X0, X1):
                row += mask_at(xx, yy, c); I[base + xx - X0 + 1] = I[prev + xx - X0 + 1] + row
        M = (I, X0, Y0, W1); best = None; c1 = max(2, round(6 * u))
        for lw0 in (72, 86):
            for lh0 in (30, 36):
                bw, bh = round(lw0 * u), round(lh0 * u)
                for by in range(Y0, Y1 - bh, c1):
                    for bx in range(X0, X1 - bw, c1):
                        r = read(feats(bx, by, bw, bh, c, M), c)
                        if not best or r[0] > best[0][0]: best = (r, bx, by, bw, bh)
        if best and best[0][0] > .78 and best[0][1] > .1:
            r, bx, by, bw, bh = best
            return r, bx + bw / 2, by + bh / 2
        return None

    for p0, c in core.items():
        if p0 in seen: continue
        stack = [p0]; seen.add(p0); pts = []
        while stack:
            p = stack.pop(); pts.append(p)
            for dx in range(-hx, hx + 1):
                for dy in range(-hy, hy + 1):
                    q = (p[0] + dx, p[1] + dy)
                    if q not in seen and core.get(q) == c: seen.add(q); stack.append(q)
        xa = min(p[0] for p in pts) * st; xb = max(p[0] for p in pts) * st + st - 1
        ya = min(p[1] for p in pts) * st; yb = max(p[1] for p in pts) * st + st - 1
        lw, lh = (xb - xa) / u, (yb - ya) / u
        if len(pts) * st * st < 150 * u * u or not (40 <= lw <= 280 and 14 <= lh <= 140): continue
        blob_sc.clear()
        if 50 <= lw <= 115 and 16 <= lh <= 48:
            # the grid box is only good to a couple of pixels: try the edges a little in and out (integral image, cheap)
            X0, Y0 = max(0, xa - 6), max(0, ya - 6); X1, Y1 = min(w, xb + 7), min(h, yb + 7); W1 = X1 - X0 + 1
            I = [0] * (W1 * (Y1 - Y0 + 1))
            for yy in range(Y0, Y1):
                row = 0; base = (yy - Y0 + 1) * W1; prev = (yy - Y0) * W1
                for xx in range(X0, X1):
                    row += mask_at(xx, yy, c); I[base + xx - X0 + 1] = I[prev + xx - X0 + 1] + row
            M = (I, X0, Y0, W1); best = None
            for dxa in (-4, -2, 0, 2):
                for dxb in (-2, 0, 2, 4):
                    for dya in (-4, -2, 0, 2):
                        for dyb in (-2, 0, 2, 4):
                            a0, b0, c0, d0 = max(X0, xa + dxa), min(X1 - 1, xb + dxb), max(Y0, ya + dya), min(Y1 - 1, yb + dyb)
                            r = read(feats(a0, c0, b0 - a0 + 1, d0 - c0 + 1, c, M), c)
                            if not best or r[0] > best[0][0]: best = (r, a0, b0, c0, d0)
            r, a0, b0, c0, d0 = best
            if r[0] > .75 and r[1] > .1:
                out.append((r[2], (a0 + b0) / 2, (c0 + d0) / 2 + r[3] * u, r[0])); continue
        sl = slide(xa, xb, ya, yb, c)
        if sl:
            r, x, y = sl; out.append((r[2], x, y + r[3] * u, r[0])); continue
        if lw <= 130 and lh <= 60:
            out.append((0, (xa + xb) / 2, (ya + yb) / 2 + 58.5 * u, 0))
        if blob_sc:        # unread: keep how well it looked like each number (for elimination by read_labels)
            cands.append(((xa + xb) / 2, (ya + yb) / 2, {nn: v for nn, v in blob_sc.items()}))
    res = {}
    for nn, x, y, sc in out:          # best one per number
        if nn and (nn not in res or sc > res[nn][2]): res[nn] = (x, y, sc)
    find_labels.cands = [(x, y + sc[nn][1] * u, nn, sc[nn][0]) for x, y, sc in cands for nn in sc]
    return [(nn, v[0], v[1]) for nn, v in res.items()] + [(0, x, y) for nn, x, y, sc in out if not nn]


def read_labels(A, data, off, w, h, deadline=None):
    """1st..4th labels, with two helps for labels the shape match could not read for sure (one half hidden behind
    another ball, an effect over it): a second look a moment later, and for each still-missing number the unread
    blob that looks most like it (a weaker match is enough once the other numbers are placed)."""
    L = find_labels(data, off, w, h); cands = list(getattr(find_labels, "cands", []))
    known = {n: (x, y) for n, x, y in L if n}
    if len(known) < 4 and (deadline is None or time.time() < deadline - 1.5):
        time.sleep(.25)
        try:
            d2, o2, w2, h2 = A.raw_frame()
            for n, x, y in find_labels(d2, o2, w2, h2):
                if n and n not in known: known[n] = (x, y)
            cands += getattr(find_labels, "cands", [])
        except Exception:
            pass
    far = lambda x, y: all(math.hypot(x - kx, y - ky) > w * .06 for kx, ky in known.values())
    for m in sorted((n for n in (1, 2, 3, 4) if n not in known),
                    key=lambda m: -max([v for x, y, nn, v in cands if nn == m] or [0])):
        best = max(((v, x, y) for x, y, nn, v in cands if nn == m and far(x, y)), default=None)
        if best and best[0] > .5:
            known[m] = (best[1], best[2])
    return [(n, x, y) for n, (x, y) in known.items()]


PHONE_TOKEN = "YOAIPXkIZdkM9kDjR1rTUVlY"
PHONE_URL = "https://square-brook-4757.naraonara123.workers.dev/st/" + PHONE_TOKEN


def phone_pusher(AU, SH):
    """every ~30 s send a small status to the tool's site, so the phone page (PHONE_URL) can show it"""
    import base64, urllib.request
    while True:
        try:
            md = time.strftime("%m/%d")
            st = {"yen": round(ai_yen(), 3), "clears": len(AU.log),
                  "orbs_today": sum(l.get("orb", 0) for l in AU.log if str(l.get("t", "")).startswith(md)),
                  "log": AU.log[-15:], "inst": []}
            for i, inf in enumerate(AU.info):
                st["inst"].append({"on": AU.run[i], "msg": inf.get("msg", ""), "clears": inf.get("clears", 0),
                                   "diff": DIFFS[min(inf.get("diff", 0), len(DIFFS) - 1)], "done": inf.get("diff", 0) >= len(DIFFS),
                                   "png": base64.b64encode(inf["png_s"]).decode() if AU.run[i] and inf.get("png_s") else ""})
            req = urllib.request.Request(PHONE_URL, data=json.dumps(st, ensure_ascii=False).encode(), method="POST",
                                         headers={"content-type": "application/json", "User-Agent": "Mozilla/5.0 monst-helper"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            pass
        time.sleep(random.uniform(25, 35))


def pause(a, b):
    """a human-like wait: usually a..b seconds, now and then a longer look"""
    time.sleep(random.uniform(a, b) if random.random() > .07 else random.uniform(b + 1.5, b + 5))


def log_line(A, msg):
    print(f"書庫自動 {A.serial}: {msg}")


def tap_back(A, w, h):
    """the blue back arrow at the top left of menu screens"""
    A.tap(w * random.uniform(.06, .11), h * random.uniform(.138, .152), random.random())


class ShukaAuto:
    """one worker per instance: look -> decide (learned or AI) -> act -> random wait. Stops on its own when unsure."""
    def __init__(self, sh, adbs):
        self.sh, self.adbs = sh, adbs
        self.run = [False] * len(adbs); sh.auto = self
        self.info = [{"msg": "停止中", "steps": 0, "clears": 0, "hist": [], "quest": ""} for _ in adbs]
        try: self.log = json.load(open(SHUKA_LOG, encoding="utf-8"))
        except Exception: self.log = []
        try: self.groups = json.load(open(SHUKA_GROUPS, encoding="utf-8"))
        except Exception: self.groups = []
        self.dbg = {}
        try: self.pos = json.load(open(SHUKA_POS, encoding="utf-8"))
        except Exception: self.pos = {}
        import threading
        for i, A in enumerate(adbs):
            threading.Thread(target=self.loop, args=(i, A), daemon=True).start()

    def thumb(self, A):
        """the list's scrollbar knob on the right edge: (x, y of its middle, track top, track bottom) or None"""
        data, off, w, h = A.raw_frame()
        xa, xb = int(w * .978), int(w * .996); ys = []; xs = []
        for y in range(int(h * .232), int(h * .868)):
            o = off + y * w * 4; hit = []
            for x in range(xa, xb):
                i = o + x * 4
                if min(data[i], data[i + 1], data[i + 2]) > 85: hit.append(x)
            if len(hit) >= (xb - xa) * .5: ys.append(y); xs.append(sum(hit) / len(hit))
        runs = []
        for y in ys:
            if runs and y - runs[-1][1] <= 4: runs[-1][1] = y
            else: runs.append([y, y])
        runs = [r for r in runs if h * .012 < r[1] - r[0] < h * .2]
        if not runs: return None
        r = max(runs, key=lambda r: r[1] - r[0]); ln = r[1] - r[0]
        x = round(sum(xs) / len(xs)); y = (r[0] + r[1]) / 2
        return x, y, h * .232 + ln / 2, h * .868 - ln / 2

    def drag_to(self, A, ty, quick=False):
        t = self.thumb(A)
        if not t: return False
        x, y, _, _ = t
        d = random.uniform(220, 420) if quick or abs(ty - y) > 200 else random.uniform(300, 650)
        A.run("shell", "input", "swipe", *map(str, (x + random.randint(-2, 2), round(y), x + random.randint(-3, 3), round(ty), round(d))), timeout=15)
        time.sleep(random.uniform(.45, .9)); return True

    def head(self, A):
        """the group heading now on top of the list"""
        data, off, w, h = A.raw_frame()
        return self.name_of(head_bits(data, off, w, h), lambda: png_crop(data, off, w, h, *HEAD_BOX))

    def name_of(self, bits, crop):
        """heading bits -> group name: learned look, else ask the AI once (only the small heading crop) and remember"""
        if sum(bits) < .2: return None
        best = min(self.groups, key=lambda G: bits_dist(bits, G["bits"]), default=None)
        if best and bits_dist(bits, best["bits"]) < .035:
            return best["name"]
        name = ask_claude_head(crop())
        with self.sh.lock: self.sh.ai_calls += 1
        if not name or "none" in name.lower(): return None
        name = norm_group(name)
        self.groups.append({"name": name, "bits": [round(v, 3) for v in bits]})
        try: json.dump(self.groups, open(SHUKA_GROUPS, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception: pass
        return name

    def survey(self, A):
        """one long drag of the scrollbar knob top -> bottom (2-3 s) while grabbing frames; the heading in each frame
        tells where each group starts. Returns [(knob y, group)] in list order."""
        import threading
        t = self.thumb(A)
        if not t: return []
        x, y, top, bot = t
        if y - top > 15:
            self.drag_to(A, top, quick=True)
        dur = random.uniform(4.5, 5.5); lag = .12
        th = threading.Thread(target=lambda: A.run("shell", "input", "swipe", *map(str, (x + random.randint(-2, 2), round(top), x + random.randint(-3, 3), round(bot), round(dur * 1000))), timeout=20), daemon=True)
        t0 = time.time(); th.start(); snaps = []
        x0, y0, x1, y1 = HEAD_BOX
        while time.time() - t0 < dur + .6:
            tt = time.time() - t0
            data, off, w, h = A.raw_frame()
            ya, yb = int(h * y0), int(h * y1)
            snaps.append((tt, bytes(data), off, w, h))
        th.join(5)
        import base64
        found = []; seen = set(); dbg = []
        for tt, data, off, w, h in snaps:
            ty = top + (bot - top) * max(0, min(1, (tt - lag) / dur))
            # the heading stuck on top (asks the AI once for a heading never seen), then the known headings
            # scrolling inside the list in the same frame: small groups that pass between two frames are not missed
            names = [self.name_of(head_bits(data, off, w, h), lambda data=data, off=off, w=w, h=h: png_crop(data, off, w, h, *HEAD_BOX))]
            if self.groups:
                for yy in range(int(h * .47), int(h * .82), 8):
                    b = head_bits(data, off, w, h, yy / h)
                    if sum(b) < .2: continue
                    G = min(self.groups, key=lambda G: bits_dist(b, G["bits"]))
                    if bits_dist(b, G["bits"]) < .045 and G["name"] not in names: names.append(G["name"])
            for g in names:
                if g and g not in seen:
                    seen.add(g); found.append((ty, g))
            dbg.append({"t": round(tt, 2), "names": [g for g in names if g], "png": base64.b64encode(png_crop(data, off, w, h, *HEAD_BOX, step=2)).decode()})
        self.span = (top, bot)
        self.dbg.setdefault(A.serial, {})["survey"] = dbg
        return found

    def heading_in_list(self, data, off, w, h, name):
        """y (pixels) of the heading row `name` scrolling inside the list (below the sticky one), or None.
        The best match there must be this very name (究極 / 究極★5以下制限 look alike at the start)."""
        if not any(G["name"] == name for G in self.groups): return None
        best = None
        for yy in range(int(h * .47), int(h * .80), 6):
            b = head_bits(data, off, w, h, yy / h)
            if sum(b) < .2: continue
            for G in self.groups:
                d = bits_dist(b, G["bits"])
                if best is None or d < best[0]: best = (d, yy, G["name"])
        if best and best[0] < .05 and best[2] == name:
            return best[1]
        return None

    def sticky(self, data, off, w, h):
        """name of the heading stuck on top of the list (learned looks only)"""
        bits = head_bits(data, off, w, h)
        if sum(bits) < .2: return None
        best = min(self.groups, key=lambda G: bits_dist(bits, G["bits"]), default=None)
        return best["name"] if best and bits_dist(bits, best["bits"]) < .04 else None

    def align(self, A, G, order, ty, span, row):
        """move the knob until G's heading sits just under the sticky heading of the group before it
        (the two headings stacked, like scrolling by hand), so its first quest is the top-left one in view"""
        step = span * .02; last = 0
        for _ in range(16):
            self.drag_to(A, ty)
            data, off, w, h = A.raw_frame()
            y = self.heading_in_list(data, off, w, h, G)
            lim = h * .84 - w * (.146 + .18 * row)
            if y is not None and y <= min(lim, h * .62):
                return ty, y
            st = self.sticky(data, off, w, h)
            if st == G and row == 0:
                t = self.thumb(A)
                if t and t[1] - t[2] < 6:          # knob at the very top: G is the first group, its heading is the top one
                    return t[1], h * .455
            past = st == G or (y is None and st in order and G in order and order.index(st) > order.index(G))
            d = -1 if past else 1
            if last and d != last: step = max(step / 2, 3)
            last = d; ty += d * step
        return ty, None

    def pick_quest(self, A, i, inf):
        """on the 書庫 list: go to the group to play and tap its first quest (no AI). Returns True when it tapped."""
        pick = inf.get("pick") or {}
        G, ty, order, span = pick.get("group"), pick.get("ty"), pick.get("order") or [], pick.get("span")
        y = None
        n = inf.setdefault("skip", {}).get(G, 0) if G else 0
        if G and ty is not None and span:      # same place as last time: after a clear the next quest slides into the same spot
            ty, y = self.align(A, G, order, ty, span, n // 5)
        if y is None:
            inf["msg"] = "書庫の一覧を見ています（スクロールバー）"; print(f"書庫自動 {A.serial}: {inf['msg']}")
            found = self.survey(A)
            order = list(dict.fromkeys(g for _, g in found))
            if found and not any(canon(g) in GROUP_KEYS for g in order):     # nothing to play? look once more before giving up
                log_line(A, f"一覧のグループ → {' / '.join(order)}（回すものが見えないので、もう一度見ます）")
                found2 = self.survey(A)
                if found2:
                    found = found2; order = list(dict.fromkeys(g for _, g in found))
            print(f"書庫自動 {A.serial}: 一覧のグループ → {' / '.join(order) or '読めませんでした'}")
            if not found:
                return self.pick_static(A, i, inf)
            G = next((g for key in GROUP_KEYS for g in order if canon(g) == key), None)
            if not G:
                self.run[i] = False; inf["msg"] = "止めました：書庫で回すクエストが残っていません（残り：" + "・".join(order) + "）"
                log_line(A, inf["msg"]); return True
            span = self.span[1] - self.span[0]; n = inf.setdefault("skip", {}).get(G, 0)
            ty = next(t for t, g in found if g == G) - span * random.uniform(.01, .02)
            ty, y = self.align(A, G, order, ty, span, n // 5)
        if y is None:
            inf["pick"] = None; return False
        data, off, w, h = A.raw_frame()
        col, row = n % 5, n // 5
        iy = y + w * (.146 + .18 * row); ix = w * (.13 + .181 * col)
        import base64
        self.dbg.setdefault(A.serial, {})["pick"] = {"png": base64.b64encode(png_small(data, off, w, h, 3)).decode(), "x": ix / w, "y": iy / h,
                                                     "head": y / h, "group": G, "order": order, "time": time.strftime("%H:%M:%S")}
        time.sleep(random.uniform(.5, 1.8))
        A.tap(ix + random.uniform(-.025, .025) * w, iy + random.uniform(-.025, .025) * w, random.random())
        inf["pick"] = {"group": G, "ty": ty, "order": order, "span": span}; inf["steps"] += 1; inf["from_list"] = True
        inf["msg"] = f"shuka_list：「{G}」の{n + 1}番目を押した"; print(f"書庫自動 {A.serial}: {inf['msg']}")
        inf["hist"] = (inf["hist"] + [f"書庫の一覧で「{G}」グループのクエストを押した" + ("（★5制限のクエスト）" if canon(G) == "star5" else "")])[-14:]
        time.sleep(random.uniform(1, 2))
        return True

    def pick_static(self, A, i, inf):
        """the list fits on one screen (no scrollbar): find a known heading on screen, by priority, and tap under it"""
        data, off, w, h = A.raw_frame()
        names = list(dict.fromkeys(G["name"] for G in self.groups))
        for key in GROUP_KEYS:
            for G in names:
                if canon(G) != key: continue
                y = self.heading_in_list(data, off, w, h, G)
                if y is None and self.sticky(data, off, w, h) == G: y = h * .455
                if y is None: continue
                n = inf.setdefault("skip", {}).get(G, 0)
                pause(.5, 1.6)
                A.tap(w * (.13 + .181 * (n % 5)) + random.uniform(-.025, .025) * w, y + w * (.146 + .18 * (n // 5)) + random.uniform(-.025, .025) * w, random.random())
                inf["pick"] = {"group": G}; inf["from_list"] = True; inf["steps"] += 1
                inf["msg"] = f"shuka_list：「{G}」の{n + 1}番目を押した（スクロールなし）"; log_line(A, inf["msg"])
                time.sleep(random.uniform(1, 2)); return True
        return False

    def save_pos(self):
        try: json.dump(self.pos, open(SHUKA_POS, "w", encoding="utf-8"))
        except Exception: pass

    def deck_box(self):
        y = self.pos.get("deck_y")
        return (.18, y - .018, .72, y + .018) if y else None

    def on_deck(self, data, off, w, h, sig=None):
        """deck select: the teal 出撃 button where it was found the first time (the top of this screen shows the quest
        name, so it looks different for every quest: only the button and the lower half are compared)"""
        low = _gray_grid(data, off, w, h, .45, .97, 8, 10)
        if any(sig_dist(low, d) < .05 for d in self.pos.get("deck_low", [])) and not is_home(data, off, w, h):
            return True
        g = self.pos.get("go")
        if g and _frac(data, off, w, h, (g[0] - .06, g[1] - .012, g[0] + .06, g[1] + .012), _TEAL) > .45:
            if not is_home(data, off, w, h) and not is_play_type(data, off, w, h) and not quest_bands(data, off, w, h) and not _search_bar(data, off, w, h):
                return True
        return is_deck_select(data, off, w, h)

    def deck_now(self, A, data, off, w, h):
        """which deck is shown. A learned look of the deck name is trusted only when it clearly matches one kind and
        clearly not the other; otherwise the AI looks at the whole screen (and the place of the name is learned again)."""
        decks = self.pos.setdefault("decknames", {}).setdefault(A.serial, [])     # [[kind, bits]]
        if self.deck_box():
            bits = deck_bits(data, off, w, h, self.deck_box())
            best = {}
            for kind, b in decks:
                d = bits_dist(bits, b); best[kind] = min(best.get(kind, 9), d)
            ok = sorted(best.items(), key=lambda kv: kv[1])
            if ok and ((len(ok) >= 2 and ok[0][1] < .04 and ok[1][1] - ok[0][1] > .03) or (len(ok) == 1 and ok[0][1] < .02)):
                return ok[0][0]
            # not sure from the look: read just the small strip with the name (a few tokens), not the whole screen
            x0, y0, x1, y1 = self.deck_box()
            kind, txt = ask_claude_deckname(png_crop(data, off, w, h, x0, y0 - .01, x1, y1 + .01))
            with self.sh.lock: self.sh.ai_calls += 1
            if kind in ("normal", "star5"):
                log_line(A, f"デッキ名を読みました（名前の部分だけ）→「{txt}」（{kind}）")
                decks.append([kind, [round(v, 3) for v in bits]]); del decks[:-12]; self.save_pos()
                return kind
        r = None
        for _ in range(2):
            r = ask_claude_deck(png_small(data, off, w, h, 3), w // 3, h // 3)
            with self.sh.lock: self.sh.ai_calls += 1
            if r: break
        if not r:
            log_line(A, "デッキ名を読めませんでした（AIの返事が読めない）"); return None
        try:
            ny = float(r["name_y"]) * 3 / h
            if abs(ny - (self.pos.get("deck_y") or ny)) > .015: decks.clear()        # the name is elsewhere: old looks are useless
            self.pos["go"] = [float(r["go"][0]) * 3 / w, float(r["go"][1]) * 3 / h]
            self.pos["deck_y"] = ny
            self.pos["deck_sig"] = [round(v, 1) for v in screen_sig(data, off, w, h)]
            gx, gy = self.pos["go"]; best = None          # the AI's point is rough: find the teal button's centre near it
            for yy in range(int(h * (gy - .06)), int(h * (gy + .06)), 6):
                for xx in range(int(w * max(.05, gx - .15)), int(w * min(.95, gx + .15)), 12):
                    f = _frac(data, off, w, h, (xx / w - .05, yy / h - .01, xx / w + .05, yy / h + .01), _TEAL, 6)
                    if best is None or f > best[0]: best = (f, xx / w, yy / h)
            if best and best[0] > .5: self.pos["go"] = [round(best[1], 4), round(best[2], 4)]
        except Exception: return None
        kind = r.get("deck")
        if r.get("quest") and r.get("name") and norm_group(r["quest"]) == norm_group(r["name"]):
            kind = None                                         # read the quest name again: not a deck name
        nm = norm_group(r.get("name") or "")
        if "書庫" in nm and ("★" in nm or "制限" in nm): kind = "star5"
        elif "書庫" in nm and "周回" in nm: kind = "normal"
        log_line(A, f"デッキ名を読みました →「{r.get('name')}」（{kind}）　クエスト名「{r.get('quest', '')}」")
        if kind is None: return None
        if kind in ("normal", "star5"):
            decks.append([kind, [round(v, 3) for v in deck_bits(data, off, w, h, self.deck_box())]]); del decks[:-12]
        self.save_pos()
        return kind

    def deck(self, A, i, inf, want):
        """deck select: make sure the shown deck is 書庫周回 / 書庫★5制限 as needed, then 出撃.
        Order of the decks: ... 書庫周回, 書庫★5制限, (デッキ5) ...  ★5 is the next deck to the RIGHT of 周回.
        To go to the deck on the right: finger right -> left. To the deck on the left: finger left -> right."""
        name = "書庫★5制限" if want == "star5" else "書庫周回"
        inf["expect_deck"] = None
        try:
            d0, o0, w0, h0 = A.raw_frame(); low = [round(v, 1) for v in _gray_grid(d0, o0, w0, h0, .45, .97, 8, 10)]
            dl = self.pos.setdefault("deck_low", [])
            if not any(sig_dist(low, d) < .03 for d in dl): dl.append(low); del dl[:-10]; self.save_pos()
        except Exception: pass
        if inf.get("deck") == want and inf.get("deck_for") == (inf.get("pick") or {}).get("group"):
            # same difficulty as the last quest: the game keeps the last deck, so no reading - straight to 出撃
            data, off, w, h = A.raw_frame()
            gx, gy = self.pos.get("go") or (.49, .825)
            pause(.5, 1.6)
            A.tap(w * (gx + random.uniform(-.04, .04)), h * (gy + random.uniform(-.008, .008)), random.random())
            inf["msg"] = f"デッキ：{name}のまま出撃"; log_line(A, inf["msg"]); inf["steps"] += 1
            inf["battle_t"] = time.time(); time.sleep(random.uniform(1.5, 2.5)); return True
        go_right = None; prev = None
        for k in range(5):
            data, off, w, h = A.raw_frame()
            if k and not self.on_deck(data, off, w, h): return True       # the screen moved on
            cur = self.deck_now(A, data, off, w, h)
            if cur is None: return False
            if cur == want:
                gx, gy = self.pos.get("go") or (.49, .825)
                pause(.5, 1.6)
                A.tap(w * (gx + random.uniform(-.04, .04)), h * (gy + random.uniform(-.008, .008)), random.random())
                inf["msg"] = f"デッキ：{name}で出撃"; log_line(A, inf["msg"])
                inf["steps"] += 1; inf["hist"] = (inf["hist"] + [f"デッキ{name}で出撃した"])[-14:]
                inf["deck"] = want; inf["deck_for"] = (inf.get("pick") or {}).get("group")
                inf["battle_t"] = time.time(); time.sleep(random.uniform(1.5, 2.5)); return True
            if cur in ("normal", "star5"):
                go_right = cur == "normal"            # 周回 -> ★5 is to the right; ★5 -> 周回 is to the left
            elif prev in ("normal", "star5"):
                go_right = not go_right               # stepped off our pair onto another deck: turn back
            elif go_right is None:
                go_right = False                      # an unknown deck to start with: try the left first
            prev = cur
            yy = h * (self.pos.get("deck_y", .45) + random.uniform(.03, .07))
            d = w * random.uniform(.36, .44); xm = w * random.uniform(.46, .54)
            x0, x1 = (xm + d / 2, xm - d / 2) if go_right else (xm - d / 2, xm + d / 2)
            pause(.4, 1.2)
            A.run("shell", "input", "swipe", *map(str, (round(x0), round(yy), round(x1), round(yy + random.uniform(-8, 8)), round(random.uniform(280, 480)))), timeout=15)
            log_line(A, f"デッキを{'右隣' if go_right else '左隣'}へ（{name}にする）"); time.sleep(random.uniform(1.1, 1.8))
        return False

    def act(self, A, a, k, seed):
        rng = random.Random(seed); kind = a["action"]
        x, y = float(a.get("x", 0)) * k, float(a.get("y", 0)) * k
        w, h = A.size or (1080, 1920)
        if kind in ("tap", "longpress"):        # a learned press would otherwise hit the very same pixel every time
            x += rng.uniform(-.012, .012) * w; y += rng.uniform(-.007, .007) * h
        if kind == "tap":
            A.tap(x, y, seed)
        elif kind == "longpress":
            A.run("shell", "input", "swipe", *map(str, (round(x + rng.uniform(-6, 6)), round(y + rng.uniform(-6, 6)),
                  round(x + rng.uniform(-3, 3)), round(y + rng.uniform(-3, 3)), round(rng.uniform(950, 1300)))), timeout=15)
        elif kind == "swipe":
            x2, y2 = float(a.get("x2", 0)) * k, float(a.get("y2", 0)) * k
            A.run("shell", "input", "swipe", *map(str, (round(x + rng.uniform(-8, 8)), round(y + rng.uniform(-8, 8)),
                  round(x2 + rng.uniform(-8, 8)), round(y2 + rng.uniform(-8, 8)), round(rng.uniform(350, 700)))), timeout=15)
        elif kind == "back":
            A.run("shell", "input", "keyevent", "4", timeout=10)

    # ------------------------------------------------------------------------------------------------
    # one worker per instance. Each round: grab the screen, then the first rule that fits acts:
    #   battle (our turn: shoot / otherwise wait) -> loading -> home -> 書庫 list -> deck select -> solo/multi
    #   -> quest page -> (anything else) a learned press, or the AI once.
    # The main road needs no AI at all; the AI only sees screens the code does not know.
    # ------------------------------------------------------------------------------------------------
    def loop(self, i, A):
        inf = self.info[i]; errs = 0
        inf.update(same=0, last_sig=None)
        while True:
            if not self.run[i]:
                time.sleep(1); continue
            try:
                if not A.size: A.connect()
                self.step(i, A, inf); errs = 0
            except Exception as e:
                errs += 1; inf["msg"] = "エラー：" + str(e)[:80]; log_line(A, inf["msg"])
                time.sleep(5)
                if errs >= 3:          # BlueStacks or adb went away: connect again
                    try: A.connect()
                    except Exception: pass
                if errs >= 30:
                    self.run[i] = False; inf["msg"] = "止めました：BlueStacks とつながりません"

    def stop(self, i, A, inf, why):
        self.run[i] = False; inf["msg"] = "止めました：" + why; log_line(A, inf["msg"])

    def battle(self, i, A, inf, data, off, w, h, k):
        t0 = time.time(); inf["battle_t"] = inf["turn_t"] = t0; inf["in_quest"] = True
        # the shot leaves within about 5 s of our turn: a short wait, the labels (a second look only if time allows),
        # and if they are not read in time, a sideways shot
        time.sleep(random.uniform(.3, 1.0))
        data, off, w, h = A.raw_frame()                     # the field may have moved while we waited
        if not turn_from_rgba(data, off, w, h): return
        L = read_labels(A, data, off, w, h, deadline=t0 + 3.8) if time.time() < t0 + 3 else []
        me = next(((x, y) for n, x, y in L if n == k), None)
        others = [(x, y) for n, x, y in L if n and n != k]
        if me and others:      # aim at the nearest other ball: friendship combos do the work
            tx, ty = min(others, key=lambda p: math.hypot(p[0] - me[0], p[1] - me[1]))
            ang = math.degrees(math.atan2(ty - me[1], tx - me[0])) + random.uniform(-3.5, 3.5)
            how = "味方を狙った"
        else:                  # labels not read: a shallow sideways shot (about 15 / 195 / -15 / 165 degrees, never exact)
            ang = random.choice((15, 195, -15, 165)) + random.uniform(-7, 7)
            how = "横向き"
        # the pull can start anywhere (never on the ball every time): wherever the thumb happens to be, mostly
        # around the middle. If the drag would not fit on screen from there, start from the middle instead.
        r = random.random()
        if r < .7: cx, cy = w * random.uniform(.3, .7), h * random.uniform(.35, .6)
        else: cx, cy = w * random.uniform(.2, .8), h * random.uniform(.25, .7)
        ms, hold = random.uniform(1000, 1450), random.uniform(60, 220)
        try:
            A.shoot(ang, cx, cy, ms, hold, True, "auto", random.random())
        except RuntimeError:
            A.shoot(ang, w * random.uniform(.4, .6), h * random.uniform(.42, .52), ms, hold, True, "auto", random.random())
        inf["steps"] += 1; inf["msg"] = f"バトル：{ORD.get(k)} {how}（{ang:.0f}°）"; log_line(A, inf["msg"])
        time.sleep(random.uniform(2.5, 3.5))     # the ball is still moving; the loop then waits for our next turn

    def rule(self, inf, name):
        """count how many times in a row the same code rule fired; after 4 the AI gets a look instead"""
        inf["wait_since"] = None
        if inf.get("h_last") == name: inf["h_n"] = inf.get("h_n", 0) + 1
        else: inf["h_last"], inf["h_n"] = name, 1
        return inf["h_n"] <= 4

    def step(self, i, A, inf):
        data, off, w, h = A.raw_frame()
        now = time.time()
        if now - inf.get("shot_t", 0) > 2.5:          # the page shows what this screen looks like right now
            inf["shot_t"] = now
            try:
                self.sh.inst[i].update(png=png_small(data, off, w, h, 6), t=int(now), state="自動", note=inf.get("msg", ""), how="")
                inf["png_s"] = png_small(data, off, w, h, 12)       # tiny one for the phone page
            except Exception: pass
        inf["force_look"] = False          # set below only for this round
        # ---- battle ----
        k = turn_from_rgba(data, off, w, h)
        if k:
            return self.battle(i, A, inf, data, off, w, h, k)
        if self.sh._panel(data, off, w, h):
            # a window over the battle with one orange button (攻略のヒントの OK): press it, no AI
            btn = [b for b in orange_buttons(data, off, w, h) if b[1] < h * .75]
            if len(btn) == 1:
                bx, by = btn[0]; pause(.5, 1.6)
                A.tap(bx + random.uniform(-.03, .03) * w, by + random.uniform(-.006, .006) * h, random.random())
                inf["msg"] = "バトル中の窓のOKを押した"; log_line(A, inf["msg"]); time.sleep(random.uniform(1, 1.8)); return
            if not inf.get("in_quest"): inf["turn_t"] = now        # a new quest: start the clock for the failsafes below
            inf["battle_t"] = now; inf["in_quest"] = True; inf["msg"] = "バトル：待ち"
            # nothing to do for long (e.g. "タップしてひろえ" at a stage end): tap the upper field now and then
            quiet = now - inf.get("turn_t", now)
            if quiet > 60 and now - inf.get("blind_t", 0) > 20:     # our turn mark may be missed: a sideways shot (ignored if not our turn)
                ang = random.choice((15, 195, -15, 165)) + random.uniform(-7, 7)
                A.shoot(ang, w * random.uniform(.4, .6), h * random.uniform(.42, .52), random.uniform(1000, 1450), random.uniform(60, 220), True, "auto", random.random())
                inf["blind_t"] = now; log_line(A, f"バトル：番が読めないまま長いので横向きに打った（{ang:.0f}°）")
            elif quiet > random.uniform(22, 32) and now - inf.get("poke_t", 0) > inf.get("poke_gap", 9):
                A.tap(w * random.uniform(.25, .75), h * random.uniform(.2, .34), random.random()); inf["poke_t"] = now
                inf["poke_gap"] = random.uniform(6, 15)
            if not (quiet > 30 and now - inf.get("look_t", 0) > 20):
                time.sleep(random.uniform(1.2, 2)); return
            inf["look_t"] = now; inf["force_look"] = True      # fall through: a learned press or the AI looks at this screen
        if inf.get("force_look"):
            pass
        elif is_loading(data, off, w, h):
            inf["msg"] = "読み込み中"; time.sleep(random.uniform(.8, 1.6)); return
        # ---- stuck on one screen? ----
        sig = screen_sig(data, off, w, h)
        inf["same"] = inf["same"] + 1 if inf["last_sig"] and sig_dist(sig, inf["last_sig"]) < .02 else 0
        inf["last_sig"] = sig; same = inf["same"]
        if same >= 15:
            return self.stop(i, A, inf, "同じ画面から進めません")
        G = (inf.get("pick") or {}).get("group")
        want = "star5" if G == "★5以下制限" or canon(G) == "star5" else "normal"
        fl = bool(inf.get("force_look"))       # looking at a battle screen for a dialog: the menu rules below stay out

        # ---- home ----
        if not fl and is_home(data, off, w, h) and same < 4 and self.rule(inf, "home"):
            if inf.get("in_quest"):          # back home after a quest: it was a clear unless we gave up
                inf["ai_n"] = 0
                if not inf.get("lost"):
                    inf["clears"] += 1
                    if G: inf.setdefault("fails", {})[G] = 0
                    q = inf.get("quest") or G or DIFFS[min(inf.get("diff", 0), len(DIFFS) - 1)]
                    self.log.append({"t": time.strftime("%m/%d %H:%M"), "i": i + 1, "quest": q, "orb": 1})
                    try: json.dump(self.log, open(SHUKA_LOG, "w", encoding="utf-8"), ensure_ascii=False)
                    except Exception: pass
                    log_line(A, f"クリア {inf['clears']} 回目（{q}）")
                inf.update(in_quest=False, lost=False, quest="")
                # people do not play non-stop: a short break now and then, a longer one every 40-80 minutes
                if not inf.get("next_rest"): inf["next_rest"] = now + random.uniform(40, 80) * 60
                rest = 0
                if now > inf["next_rest"]:
                    rest = random.uniform(3, 10) * 60; inf["next_rest"] = now + rest + random.uniform(40, 80) * 60
                elif random.random() < .08:
                    rest = random.uniform(20, 120)
                if rest:
                    inf["msg"] = f"休憩中（約{round(rest / 60, 1)}分）"; log_line(A, inf["msg"])
                    end = time.time() + rest
                    while time.time() < end and self.run[i]: time.sleep(2)
                    return
            pause(.8, 2.5)
            if inf.get("played"):            # long press: jumps to the 書庫 quest we last played, then back to the list
                # on the クエスト letters / just under them; the SHORT CUT band (y .72-.77) jumps somewhere else
                x, y = w * random.uniform(.45, .55), h * random.uniform(.80, .835)
                A.run("shell", "input", "swipe", *map(str, (round(x), round(y), round(x + random.uniform(-4, 4)), round(y + random.uniform(-4, 4)), round(random.uniform(950, 1300)))), timeout=15)
                inf["after_lp"] = True; inf["lp_t"] = time.time(); inf["lp_backs"] = 0; inf["msg"] = "ホーム：クエストを長押し"
            else:                            # first time: a plain tap on the lower half (the SHORT CUT part jumps elsewhere)
                A.tap(w * random.uniform(.44, .56), h * random.uniform(.825, .855), random.random())
                inf["msg"] = "ホーム：クエストをタップ"
            log_line(A, inf["msg"]); inf["steps"] += 1; inf["hist"] = (inf["hist"] + [inf["msg"]])[-14:]
            time.sleep(random.uniform(1.5, 2.5)); return

        # ---- 書庫 list: filtered to one 難易度 + 報酬未受取, the next quest is always the top-left one ----
        isl = not fl and is_shuka_list(data, off, w, h)
        inf["list_n"] = inf.get("list_n", 0) + 1 if isl else 0
        if isl and inf["list_n"] < 2 and not _search_bar(data, off, w, h):
            time.sleep(random.uniform(.6, 1)); return     # looks like the list without its search bar: make sure on the next look
        if isl:
            inf["after_lp"] = False
            di = inf.get("diff", 0)
            if di >= len(DIFFS):
                return self.stop(i, A, inf, "極・★5以下制限・究極・激究極のクエストは全部終わりました")
            D = DIFFS[di]
            inf["replay_wait"] = 0
            if inf.get("replay") is not None:       # back on the list after replaying the filter presses
                if not inf["replay"]:
                    inf["filter_for"] = inf.pop("filter_target", None); log_line(A, f"絞り込み：覚えた押し方で「{inf['filter_for']}」にしました")
                else:                               # stopped half way: forget those presses, the AI does it next time
                    self.pos.get("filter_seq", {}).pop(D, None); self.save_pos()
                inf["replay"] = None
            if same < 4 and self.rule(inf, "list"):
                f = first_icon(data, off, w, h)
                if inf.get("filter_for") != D:
                    tries = inf.setdefault("filter_try", {}); tries[D] = tries.get(D, 0) + 1
                    if tries[D] > 4:
                        return self.stop(i, A, inf, f"絞り込みを「{D}」にできませんでした")
                    # press 表示順変更 ourselves (not the round 一覧 next to it); the AI then works the filter window
                    inf["need_filter"] = True; inf["filter_target"] = D
                    # the list alone cannot show whether 報酬未受取 is on: open the filter every time it is not confirmed yet
                    inf["hist"] = (inf["hist"] + [f"絞り込みの確認：「絞り込み」で 難易度が「{D}」、初クリア報酬が「報酬未受取」になっているか見て、違えば直して「決定」"])[-14:]
                    pause(.6, 1.6); A.tap(w * random.uniform(.68, .84), h * random.uniform(.125, .145), random.random())
                    seq = self.pos.get("filter_seq", {}).get(D)
                    inf["fsess"] = None if seq else {"D": D, "taps": []}     # record the AI's presses only when there is nothing to replay
                    inf["replay"] = [list(p) for p in seq] if seq else None
                    log_line(A, f"表示順変更を開いた（難易度を「{D}」にする{'・覚えた押し方で' if seq else ''}）"); time.sleep(random.uniform(1.2, 2)); return
                if f is None:
                    # looks empty - but a list that is still loading or sliding in looks the same: only after
                    # 3 looks in a row over a few seconds, with the search bar showing, is it really empty
                    inf["empty_n"] = inf.get("empty_n", 0) + 1
                    if inf["empty_n"] < 3 or (not _search_bar(data, off, w, h) and inf["empty_n"] < 6):
                        inf["msg"] = f"一覧が空に見える（{inf['empty_n']}回目の確認）"; time.sleep(random.uniform(1.2, 2)); return
                    inf["empty_n"] = 0
                    # before moving on, make sure there really is no 極 (etc.) quest on the list: a heading still shown,
                    # or the AI seeing a quest, means it is not empty
                    # the heading shows the count even at 0: read it (small crop of the heading band)
                    cnt = ask_claude_count(png_crop(data, off, w, h, 0, .40, 1, .52))
                    with self.sh.lock: self.sh.ai_calls += 1
                    if cnt is not None:
                        left = cnt > 0
                        log_line(A, f"「{D}」の見出しの件数：{cnt}")
                    else:
                        left = ask_claude_any_quest(png_small(data, off, w, h, 3), D)
                        with self.sh.lock: self.sh.ai_calls += 1
                    if left is not False:
                        log_line(A, f"AIの確認：「{D}」のクエストがまだ一覧にある（または確認できない）ので続けます")
                        inf["msg"] = "一覧の確認待ち"; time.sleep(2); return
                    inf["diff"] = di + 1; inf["filter_for"] = None
                    log_line(A, f"「{D}」は全部終わりました → 次は「{DIFFS[di + 1] if di + 1 < len(DIFFS) else 'なし'}」")
                    return
                inf["empty_n"] = 0
                n = inf.setdefault("skip", {}).get(D, 0)        # quests lost twice are skipped: the next one to the right
                pause(.6, 1.8)
                A.tap(w * (.13 + .181 * (n % 5)) + random.uniform(-.03, .03) * w, f[1] + w * .18 * (n // 5) + random.uniform(-.03, .03) * w, random.random())
                inf["pick"] = {"group": D}; inf["from_list"] = True; inf["steps"] += 1; inf.setdefault("filter_try", {})[D] = 0
                inf["msg"] = f"一覧：「{D}」の{n + 1}番目を押した"; log_line(A, inf["msg"])
                inf["hist"] = (inf["hist"] + [inf["msg"]])[-14:]; time.sleep(random.uniform(1.2, 2)); return
            return            # the list is ours: never ask the AI about it
        else:
            inf["_pickfail"] = False

        # ---- after the long press we expect the cleared quest's page; if the SHORT CUT fired instead we are on the
        #      solo / helper / deck screens of the old quest: never start it again, go back until the list ----
        if not fl and inf.get("after_lp") and not is_shuka_list(data, off, w, h) and not quest_bands(data, off, w, h):
            if self.on_deck(data, off, w, h, sig) or is_play_type(data, off, w, h) or (now - inf.get("lp_t", now) > 4):
                inf["lp_backs"] = inf.get("lp_backs", 0) + 1
                if inf["lp_backs"] <= 5:
                    pause(.6, 1.6); tap_back(A, w, h)
                    inf["msg"] = "長押しで出撃直前まで飛んだので戻る"; log_line(A, inf["msg"])
                    time.sleep(random.uniform(1.2, 2)); return
                inf["after_lp"] = False

        # ---- deck select ----
        if fl:
            pass
        elif self.on_deck(data, off, w, h, sig) and same < 3 and self.rule(inf, "deck"):
            if self.deck(A, i, inf, want): return
            inf["hist"] = (inf["hist"] + [f"デッキを{'書庫★5制限' if want == 'star5' else '書庫周回'}にしたいが、プログラムでは見つからなかった"])[-14:]

        # ---- solo / multi ----
        elif is_play_type(data, off, w, h) and same < 3 and self.rule(inf, "solo"):
            pause(.6, 1.8)
            A.tap(w * random.uniform(.15, .36), h * random.uniform(.47, .6), random.random())
            inf["msg"] = "ソロを押した"; log_line(A, inf["msg"]); inf["steps"] += 1
            inf["hist"] = (inf["hist"] + ["ソロを押した"])[-14:]; time.sleep(random.uniform(1.2, 2)); return

        # ---- quest page (difficulty bands) ----
        else:
            # a quest page: two or more difficulty bands (or one band under the 書庫 header). A lone teal patch on
            # a battle screen is not one.
            bands = [] if is_shuka_list(data, off, w, h) else quest_bands(data, off, w, h)
            if len(bands) == 1 and not shuka_header(data, off, w, h): bands = []
            if bands and same < 3 and self.rule(inf, "quest"):
                if inf.get("after_lp"):          # reached by the long press from home: back to the list
                    inf["after_lp"] = False; pause(.6, 1.8); tap_back(A, w, h)
                    inf["msg"] = "長押しで来たクエストのページ：戻る"
                elif inf.get("from_list") and has_clear_tag(data, off, w, h):
                    # a quest whose reward is already taken was in the list: the 報酬未受取 filter is off.
                    # The first time: back and fix the filter. If it happens again anyway: just skip that quest.
                    inf["from_list"] = False; pause(.6, 1.6); tap_back(A, w, h)
                    inf["clear_seen"] = inf.get("clear_seen", 0) + 1
                    if inf["clear_seen"] == 1:
                        self.pos.get("filter_seq", {}).pop(G, None); self.save_pos()
                        inf["filter_for"] = None; inf["msg"] = "CLEAR済みのクエスト：戻って絞り込みを直す"
                        inf["hist"] = (inf["hist"] + ["一覧にCLEAR済みが出ていた。絞り込みが必要：表示順変更→初クリア報酬を「報酬未受取」→決定"])[-14:]
                    else:
                        if G: inf.setdefault("skip", {})[G] = inf["skip"].get(G, 0) + 1
                        inf["msg"] = "CLEAR済みのクエスト：戻って隣のクエストにする"
                else:                            # the top band is the hardest difficulty: play that one
                    inf["from_list"] = False; inf["played"] = True
                    pause(.6, 1.8)
                    A.tap(w * random.uniform(.22, .55), bands[0] + h * random.uniform(-.008, .008), random.random())
                    inf["msg"] = f"クエストのページ：一番上の難易度（{G or '?'}）"
                log_line(A, inf["msg"]); inf["steps"] += 1; inf["hist"] = (inf["hist"] + [inf["msg"]])[-14:]
                time.sleep(random.uniform(1.2, 2)); return

        # ---- the filter window, when its presses for this difficulty were learned: replay them (no AI) ----
        if inf.get("replay") is not None and not is_shuka_list(data, off, w, h):
            if inf["replay"]:
                x, y = inf["replay"].pop(0)
                pause(.5, 1.4); A.tap(w * (x + random.uniform(-.012, .012)), h * (y + random.uniform(-.006, .006)), random.random())
                inf["msg"] = "絞り込み：覚えた押し方で操作中"; time.sleep(random.uniform(.9, 1.5)); return
            # all presses done but the list is not back yet: give it a few looks (the window may be closing)
            inf["replay_wait"] = inf.get("replay_wait", 0) + 1
            if inf["replay_wait"] <= 3:
                time.sleep(random.uniform(.8, 1.3)); return
            inf["replay"] = None; inf["replay_wait"] = 0       # still open: the AI looks (it may need one more press)

        # ---- anything else: a learned press for this exact look, or the AI ----
        fs = full_sig(data, off, w, h)
        L = min((L for L in self.sh.learn if L.get("act") and sig_dist(sig, L["sig"]) < .05
                 and (L["act"].get("full") is not None)), key=lambda L: sig_dist(fs, L["act"]["full"]), default=None)
        if L:
            # screens whose middle text changes every time but whose button stays put: a looser match
            wy = L["act"].get("why") or ""
            loose = L.get("state") == "result" or "ヒント" in wy or "EXTRA" in wy.upper()
            if sig_dist(fs, L["act"]["full"]) > (.16 if loose else .09): L = None
        if L and same >= 3:                      # pressed it and nothing changed: forget it
            with self.sh.lock: L.pop("act", None); self.sh.save()
            L = None
        if L:
            a = dict(L["act"]); how = "覚えた操作"
            wy = a.get("why") or ""
            if a.get("action") == "tap" and ("ヒント" in wy or "EXTRA" in wy.upper() or "諦め" in wy):
                btn = orange_buttons(data, off, w, h)
                if btn:
                    # EXTRA: the left one is 諦める (挑戦する is on the right); the hint has one OK
                    bx, by = min(btn, key=lambda p: p[0]) if ("EXTRA" in wy.upper() or "諦め" in wy) else btn[-1]
                    a.update(x=bx, y=by, v=2); how = "覚えた操作・ボタン位置を見て"
            if a.get("v") != 2:                  # learned before v76: coordinates on the half-size image
                for x in ("x", "y", "x2", "y2"):
                    if a.get(x) is not None: a[x] = float(a[x]) * 2
        else:
            # right after the helper was chosen the next screen is the deck select. When the deck is already known for
            # this difficulty, never ask the AI about it: press 出撃 where it was found (if a teal button is there).
            if inf.get("expect_deck") and now - inf["expect_deck"] < 20 and inf.get("deck") == want \
                    and inf.get("deck_for") == G and self.pos.get("go"):
                gx, gy = self.pos["go"]
                if _frac(data, off, w, h, (gx - .08, gy - .02, gx + .08, gy + .02), _TEAL) > .25:
                    pause(.5, 1.5); A.tap(w * (gx + random.uniform(-.04, .04)), h * (gy + random.uniform(-.008, .008)), random.random())
                    inf["expect_deck"] = None; inf["battle_t"] = time.time()
                    inf["msg"] = f"デッキ：{'書庫★5制限' if want == 'star5' else '書庫周回'}のまま出撃"; log_line(A, inf["msg"]); inf["steps"] += 1
                    time.sleep(random.uniform(1.5, 2.5)); return
                time.sleep(random.uniform(.8, 1.4)); return          # not shown yet: look again (no AI)
            # a window with exactly one orange button (攻略のヒントの OK, リザルトの OK, 日付が変わりました ...):
            # that button only moves things on. Press it ourselves, wherever the text pushed it - no AI.
            if _frac(data, off, w, h, (.1, .4, .42, .6), _BLUE) < .2:
                btn = orange_buttons(data, off, w, h)
                if len(btn) == 1 and same < 3:
                    bx, by = btn[0]; pause(.5, 1.6)
                    A.tap(bx + random.uniform(-.03, .03) * w, by + random.uniform(-.006, .006) * h, random.random())
                    inf["msg"] = "オレンジのボタン（OKなど）を押した"; log_line(A, inf["msg"]); inf["steps"] += 1
                    inf["hist"] = (inf["hist"] + [inf["msg"]])[-14:]; time.sleep(random.uniform(1, 1.8)); return
            if now - inf.get("battle_t", 0) < 20 and not inf.get("force_look"):     # just after battle activity
                inf["msg"] = "バトル：演出中"; time.sleep(random.uniform(1.2, 2)); return
            # only ask about a screen that holds still (no AI for screens in the middle of an animation)
            time.sleep(.7)
            d2, o2, w2, h2 = A.raw_frame()
            if sig_dist(full_sig(d2, o2, w2, h2), fs) > .03 and same < 6:
                inf["msg"] = "画面が動いています"; return
            hist = [f"この周回でクリアした数: {int(inf['clears'])}"] + inf["hist"] + ([f"※ 同じ画面が{same}回続いている。さっきと違う操作を試すこと"] if same >= 2 else [])
            if inf.get("wait_since") and now - inf["wait_since"] > 30:
                hist.append("※ 30秒以上「待つ」が続いている。「通信中」の表示は気にせず、画面にあるボタン（OK・閉じる・次へ・諦める など）を押すこと。wait は答えない")
            # safety: the AI is for the odd screen. Leaning on it again and again means the code lost its way:
            # stop instead of letting the AI drive (10 calls without a clear in between)
            inf["ai_n"] = inf.get("ai_n", 0) + 1
            if inf["ai_n"] > 10:
                return self.stop(i, A, inf, "クリアの間にAIへ10回以上聞いたので、安全のため止めました（ログを見せてください）")
            a, err = ask_claude_action(png_small(data, off, w, h, 3), w // 3, h // 3, hist)
            with self.sh.lock: self.sh.ai_calls += 1
            if not a:
                inf["msg"] = "AI：" + err; time.sleep(8); return
            how = "AI"
            for x in ("x", "y", "x2", "y2"):     # AI coordinates are on the third-size image
                if a.get(x) is not None: a[x] = float(a[x]) * 3
            if a.get("fixed") and a["action"] in ("tap", "longpress") and a.get("state") not in ("home", "shuka_list", "quest_detail", "play_type", "deck_select", "filter", "battle", "loading", "other"):
                with self.sh.lock:
                    self.sh.add(a.get("state", "other"), a.get("why", ""), sig, png_small(data, off, w, h, 6))
                    self.sh.learn[-1]["act"] = dict({x: a.get(x) for x in ("action", "x", "y", "x2", "y2", "state", "why")}, full=[round(v, 1) for v in fs], v=2)
                    self.sh.save()
        st = a.get("state", "?"); why = a.get("why", ""); inf["h_last"] = None
        if st == "deck_select" and how == "AI" and not inf.get("_deckai"):
            # the AI says this is the deck select screen: let the code read the deck (and learn where things are) once
            inf["_deckai"] = True
            if self.deck(A, i, inf, want): inf["_deckai"] = False; return
        elif st != "deck_select": inf["_deckai"] = False
        # never spend orbs / money, never continue
        if a["action"] in ("tap", "longpress") and any(t in why for t in ("購入", "コンティニュー", "オーブを使", "回復する")) and "あきらめ" not in why:
            return self.stop(i, A, inf, f"危ない操作をしそうになったので止めました（{why}）")
        if a["action"] in ("tap", "longpress") and ("挑戦する" in why or ("EXTRA" in why.upper() and not any(t in why for t in ("諦", "あきら", "しない", "閉じ")))):
            inf["hist"] = (inf["hist"] + ["EXTRA STAGEには挑戦しない。「諦める」の方を押すこと"])[-14:]
            log_line(A, f"EXTRAに挑戦しそうだったので押しませんでした（{why}）"); time.sleep(1); return
        if a["action"] == "wait":
            if not inf.get("wait_since"): inf["wait_since"] = now
        else:
            inf["wait_since"] = None
        if st == "lose":
            if not inf.get("lost") and G:
                f = inf.setdefault("fails", {}); f[G] = f.get(G, 0) + 1
                if f[G] >= 2:
                    f[G] = 0; inf.setdefault("skip", {})[G] = inf["skip"].get(G, 0) + 1
                    log_line(A, f"「{G}」のこのクエストは2回負けたので、次からは隣のクエストにします")
            inf["lost"] = True
        if st == "battle" or "出撃" in why: inf["battle_t"] = now
        if a.get("quest"): inf["quest"] = str(a["quest"])[:40]
        if st == "shuka_list" and a["action"] == "tap" and float(a.get("x") or 0) > w * .875 and float(a.get("y") or 0) < h * .2:
            # the round 一覧 button right of 表示順変更 opens another window: press 表示順変更 instead
            a["x"], a["y"] = w * random.uniform(.68, .84), h * random.uniform(.125, .145); why += "（一覧ボタンではなく表示順変更）"
        if st == "filter" and a["action"] == "tap" and inf.get("fsess") is not None:
            inf["fsess"]["taps"].append([round(float(a["x"]) / w, 4), round(float(a["y"]) / h, 4)])
        if st == "filter" and "決定" in why and inf.get("filter_target"):
            fs_ = inf.pop("fsess", None)
            if fs_ and 1 <= len(fs_["taps"]) <= 6:      # remember these presses for this difficulty
                self.pos.setdefault("filter_seq", {})[fs_["D"]] = fs_["taps"]; self.save_pos()
            inf["filter_for"] = inf.pop("filter_target"); inf["need_filter"] = False
            log_line(A, f"絞り込み：難易度「{inf['filter_for']}」・報酬未受取にしました")
        inf["msg"] = f"{st}：{a['action']}（{how}）{why}"; log_line(A, inf["msg"])
        if a["action"] == "stop":
            return self.stop(i, A, inf, why or st)
        if a["action"] == "wait":
            time.sleep(random.uniform(1, 2.5)); return
        pause(.6, 2.2) if how == "AI" else pause(1, 3)
        if st == "helper_select": inf["expect_deck"] = time.time()
        self.act(A, a, 1, random.random())
        inf["steps"] += 1; inf["hist"] = (inf["hist"] + [f"{st}で{a['action']}（{why}）"])[-14:]
        time.sleep(random.uniform(.8, 1.6))


SHUKA_PAGE = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>書庫周回</title><style>
:root{--bg:#0e1014;--card:#171a21;--line:#262b36;--tx:#e8eaef;--mut:#8b93a3;--go:#3ddc84;--warn:#ffcf3f;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
header{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2}
h1{font-size:19px;margin:0}.sum{display:flex;gap:18px;font-size:14px}.sum b{font-size:22px;color:var(--warn);margin-right:3px}
button{font:inherit;font-size:14px;border-radius:10px;border:1px solid var(--line);background:#1f2430;color:var(--tx);padding:9px 14px;cursor:pointer}
button.go{background:#1f5a3a;border-color:var(--go)}button.stop{background:#4a2326;border-color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;padding:18px 20px}
.c{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:10px}
.top{display:flex;justify-content:space-between;align-items:center}.nm{font-weight:700}.pill{font-size:12px;padding:3px 10px;border-radius:99px;background:#2a2f3a;color:var(--mut)}
.pill.run{background:#173d2a;color:var(--go)}.pill.rest{background:#3d3517;color:var(--warn)}.pill.stopd{background:#3d1a1d;color:var(--bad)}
.c img{width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:10px;background:#000}
.kv{display:flex;gap:12px;font-size:13px;color:var(--mut)}.kv b{color:var(--tx);font-size:15px}
.msg{font-size:13px;line-height:1.5;min-height:3em;color:#cfd4dd}
section{margin:6px 20px 20px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
h2{font-size:15px;margin:0 0 10px}table{width:100%;border-collapse:collapse;font-size:13px}td{padding:6px 8px;border-top:1px solid var(--line)}
.lg{max-height:300px;overflow:auto}details summary{cursor:pointer;color:var(--mut);font-size:13px}.small{font-size:12px;color:var(--mut)}
.L{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;margin-top:10px}.L div{font-size:12px}.L img{width:100%;border-radius:6px}
input,select{font:inherit;font-size:13px;border-radius:8px;border:1px solid var(--line);background:#1f2430;color:var(--tx);padding:7px 10px}
</style></head><body>
<header><h1>書庫周回</h1><div class="sum"><span>きょうのオーブ <b id="orbs">0</b></span><span>AI料金 <b id="yen" style="font-size:16px">0円</b></span></div>
<span style="flex:1"></span><button id="all" class="go">ぜんぶ回す</button><button id="none" class="stop">ぜんぶ止める</button></header>
<div class="grid" id="m"></div>
<section><h2 id="lh">クリアしたクエスト</h2><div class="lg"><table id="LG"></table></div></section>
<section><details><summary>くわしい設定・調べもの（ふだんは触らなくてOK）</summary>
<p class="small" id="keybox"></p>
<p><button id="run">画面の判定だけ始める</button> <a href="/shuka/debug" target="_blank" class="small" style="color:#8bd">一覧の読み取りを見る</a></p>
<p class="small" id="info"></p><h2 style="margin-top:12px">覚えた画面（違っていたら直す・消す）</h2><div class="L" id="L"></div></details></section>
<script>
let S=null;const $=id=>document.getElementById(id);
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json();}
async function tick(){try{S=await (await fetch('/shuka/state')).json();render();}catch(e){$('m').innerHTML='<p class="small" style="padding:20px">ヘルパーにつながりません（start_helper.bat が動いているか確認）</p>';}}
function pill(a){if(!a.on)return a.msg&&a.msg.startsWith('止めました')?['stopd','止まりました']:['','停止中'];if((a.msg||'').startsWith('休憩'))return['rest','休憩中'];return['run','動いています'];}
function render(){
 const today=new Date();const md=String(today.getMonth()+1).padStart(2,'0')+'/'+String(today.getDate()).padStart(2,'0');
 const lg=S.log||[];$('orbs').textContent=lg.filter(l=>(l.t||'').startsWith(md)).reduce((a,b)=>a+(b.orb||0),0);
 $('yen').textContent=`約${S.yen.toFixed(2)}円`;
 $('m').innerHTML=S.inst.map((s,i)=>{const a=(S.auto||[])[i]||{};const [pc,pt]=pill(a);
  return `<div class="c"><div class="top"><span class="nm">${i+1}画面目</span><span class="pill ${pc}">${pt}</span></div>
  <img src="/shuka/thumb?i=${i}&t=${s.t}" alt="">
  <div class="kv"><span>いまの難易度 <b>${a.done?'全部おわり':esc(a.diff||'極')}</b></span><span>クリア <b>${a.clears||0}</b></span></div>
  <div class="msg">${esc(a.on||a.msg?a.msg:s.state)}</div>
  <button data-a="${i}" class="${a.on?'stop':'go'}">${a.on?'止める':'回す'}</button>
  <div class="small">${esc(s.serial)}</div></div>`;}).join('');
 document.querySelectorAll('button[data-a]').forEach(b=>b.onclick=async()=>{const i=+b.dataset.a;await post('/shuka/auto',{i,on:!((S.auto||[])[i]||{}).on});tick();});
 $('lh').textContent=`クリアしたクエスト ${lg.length}個 ・ オーブ ${lg.reduce((a,b)=>a+(b.orb||0),0)}個`;
 $('LG').innerHTML=lg.length?lg.slice().reverse().map(l=>`<tr><td>${esc(l.t)}</td><td>${l.i}画面目</td><td>${esc(l.quest)}</td><td>+${l.orb}オーブ</td></tr>`).join(''):'<tr><td class="small">まだありません</td></tr>';
 $('run').textContent=S.on?'画面の判定を止める':'画面の判定だけ始める';
 $('info').textContent=`覚えた画面 ${S.learned} ・ AIに聞いた回数 ${S.ai_calls}`;
 if(S.key)$('keybox').textContent='APIキー：設定済み';
 else if(!$('k')){$('keybox').innerHTML='APIキー未設定 <input id="k" type="password" placeholder="sk-ant-..." style="width:200px"> <button id="ks">保存</button>';
  $('ks').onclick=async()=>{const r=await post('/shuka/key',{key:$('k').value.trim()});if(!r.ok)alert(r.error);else{$('keybox').textContent='APIキー：設定済み';tick();}};}
 const opts=Object.entries(S.states).map(([k,v])=>`<option value="${k}">${k}：${esc(v)}</option>`).join('');
 const sig=S.learn.map(l=>l.id+l.state).join();if(sig!==window._ls){window._ls=sig;
  $('L').innerHTML=S.learn.map(l=>`<div><b>${esc(l.state)}</b> <span class="small">${l.n}回</span><img src="/shuka/limg?id=${l.id}" alt=""><select data-l="${l.id}"><option value="">直す…</option>${opts}<option value="_delete">この見本を消す</option></select></div>`).join('');
  document.querySelectorAll('select[data-l]').forEach(x=>x.onchange=async()=>{if(x.value){await post('/shuka/relabel',{id:x.dataset.l,state:x.value});window._ls='';tick();}});}}
$('run').onclick=async()=>{await post('/shuka/run',{on:!S.on});tick();};
$('all').onclick=async()=>{for(let i=0;i<S.inst.length;i++){if(!((S.auto||[])[i]||{}).on){await post('/shuka/auto',{i,on:true});await new Promise(r=>setTimeout(r,1500+Math.random()*2500));}}tick();};
$('none').onclick=async()=>{for(let i=0;i<S.inst.length;i++)await post('/shuka/auto',{i,on:false});tick();};
tick();setInterval(()=>{if(!document.querySelector('select:focus,input:focus'))tick();},3000);
</script></body></html>"""



def self_update():
    """download the newest helper from the tool's site; replace this file if it is newer. True = updated (restart needed)"""
    import re, urllib.request
    try:
        req = urllib.request.Request(f"{UPDATE_URL}?t={int(time.time())}", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) monst-helper"})
        with urllib.request.urlopen(req, timeout=8) as r:   # the site refuses Python's default user agent
            code = r.read().decode("utf-8")
        m = re.search(r"^HELPER_VER = (\d+)", code, re.M)
        if not m or int(m.group(1)) <= HELPER_VER:
            return False
        compile(code, "monst_helper.py", "exec")          # never install something broken
        me = os.path.abspath(__file__)
        shutil.copyfile(me, me + ".bak")
        with open(me, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(code)
        print(f"ヘルパーを v{HELPER_VER} → v{m.group(1)} に更新しました。再起動します…")
        return True
    except Exception as e:
        print(f"（更新の確認をスキップ: {e}）")
        return False


def restart_for_update():
    try: open(RESTART_FLAG, "w").write("1")
    except Exception: pass
    sys.stdout.flush(); os._exit(3)    # start_helper.bat starts it again


def main():
    global KEY_FILE
    KEY_FILE = os.path.join(HERE, "apikey.txt")
    if self_update():
        restart_for_update()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None, help="BlueStacks の ADB ポート（複数の画面に同時に打つならカンマ区切り: 5575,5585,5595）")
    ap.add_argument("--adb", default=None, help="adb.exe の場所（省略すると自動で探す）")
    a = ap.parse_args()
    exe = a.adb or find_adb()
    if not exe:
        print("adb が見つかりません。--adb で HD-Adb.exe の場所を指定してください"); sys.exit(1)
    port = a.port
    restarted = os.path.exists(RESTART_FLAG)
    if restarted:
        try: os.remove(RESTART_FLAG)
        except Exception: pass
    if not port and restarted and os.path.exists(os.path.join(HERE, "ports.txt")):
        port = open(os.path.join(HERE, "ports.txt")).read().strip()   # after an update: same screens, no question
    if not port:   # ask here (cmd.exe garbles long Japanese prompts in .bat files)
        last_f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ports.txt")
        last = open(last_f).read().strip() if os.path.exists(last_f) else ""
        if "," not in last:
            last = "5575,5555,5565"   # the 3 BlueStacks screens
        try:
            port = input(f"BlueStacks の ADB ポート番号（複数の画面ならカンマ区切り 例 5575,5555,5565）\nそのまま Enter で前回の {last}: ").strip() or last
        except EOFError:
            port = last
        try: open(last_f, "w").write(port)
        except Exception: pass
    ports = [p.strip() for p in str(port).replace("、", ",").replace("，", ",").replace(" ", ",").split(",") if p.strip()]
    adbs = [Adb(exe, f"127.0.0.1:{p}") for p in ports]
    for i, A in enumerate(adbs):
        try:
            w, h = A.connect(); A.touch()
            print(f"BlueStacks に接続しました（{A.serial}、画面 {w}x{h}）" + ("  ← 画面取り込みはこれ" if i == 0 else "  ← 同時に打つ"))
        except Exception as e:
            print(f"{A.serial} にまだ接続できていません: {e}\n（BlueStacks を起動して ADB をオンにしてください）")
    print(f"ヘルパー v{HELPER_VER} 起動中: http://127.0.0.1:{LISTEN}  （このウィンドウを閉じると止まります）")
    print(f"書庫周回の画面判定: http://127.0.0.1:{LISTEN}/shuka をブラウザで開く")
    print(f"スマホで見る: {PHONE_URL}")
    ThreadingHTTPServer(("127.0.0.1", LISTEN), make_handler(adbs)).serve_forever()


if __name__ == "__main__":
    main()
