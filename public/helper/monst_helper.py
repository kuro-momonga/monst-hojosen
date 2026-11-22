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
import argparse, json, math, os, random, shutil, struct, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = 8765
HELPER_VER = 64
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
        print("dd:", err.replace("\n", " | ")[:200])
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
                if self.path.startswith("/shuka/state"):
                    return self.reply(200, {**SH.status(), "log": AU.log[-300:], "auto": [{"on": AU.run[i], **{k: v for k, v in AU.info[i].items() if k != "hist"}} for i in range(len(adbs))]})
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
    u = j.get("usage") or {}
    AI_USE["in"] += int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0)
    AI_USE["out"] += int(u.get("output_tokens", 0))


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
SHUKA_GOAL = """あなたはスマホゲーム「モンスターストライク」をプレイしています。目的は「追憶の書庫」で、まだクリア報酬（オーブ）を受け取っていないクエストを1つずつクリアしていくことです。
ルール：
- ホーム画面：この周回でまだ書庫のクエストを1つもクリアしていなければ、真ん中の丸い「クエスト」ボタンの下半分（「クエスト」の文字かその下の「コラボ開催中」あたり）を普通にタップし、クエスト一覧で「追憶の書庫」を押す。ボタン上部の「SHORT CUT」の矢印のあたりや長押しは、関係ない前回のクエスト（禁忌の獄など）へ飛ぶので使わない。間違って書庫以外に入ったら、左上の青い戻る矢印やホームボタンで戻る。書庫のクエストを1つ以上クリアした後は「クエスト」を約1秒長押しする（さっきの書庫クエストの詳細へ飛ぶので、左上の戻る矢印で書庫の一覧に戻る）。
- クエスト一覧では「追憶の書庫」を押す。
- 書庫の一覧で絞り込みが「報酬未受取」になっていなければ、右上の「表示順変更」→ 初クリア報酬を「報酬未受取」→「決定」。
- 書庫の一覧は上から難しい順（黎絶・轟絶・爆絶・超絶…）に並んでいて、極・究極などは一番下のほうにある。極 → 究極 → ★5制限 → 激究極 のグループの順に、そのグループの一番上のクエストを選ぶ。見えなければ下へスクロールする（swipe で y2 を y より小さく。スクロールはプログラムが一番下まで一気に行う）。一番下までスクロール済みで、超絶以上の難しいグループしか残っていないときだけ止める（stop）。
- クエスト詳細では、一番上の難易度を押す。クエスト名の帯に「CLEAR!」が付いていたら、左上の青い戻る矢印で一覧に戻る。
- 助っ人選択では「現在のフレンド使用キャラ」の一番上を押す。
- デッキ選択では、デッキ名が★5制限のクエストなら「書庫★5制限」、それ以外は「書庫周回」になっているか確かめる。違えばデッキ欄（左右の青い矢印の間）を左右にスワイプして切り替える。合っていれば「出撃」。
- 読み込み中・通信中・演出中は待つ（wait）。
- リザルトやスペシャル報酬は画面を押して進める。OKボタンがあれば押す。
- 確認ウィンドウはOKを押す（購入・オーブ消費・コンティニューは押さない）。
- 負けた画面では「あきらめる」を押す。
- 分からない・危ない画面では stop。
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


class ShukaAuto:
    """one worker per instance: look -> decide (learned or AI) -> act -> random wait. Stops on its own when unsure."""
    def __init__(self, sh, adbs):
        self.sh, self.adbs = sh, adbs
        self.run = [False] * len(adbs); sh.auto = self
        self.info = [{"msg": "停止中", "steps": 0, "clears": 0, "hist": [], "quest": ""} for _ in adbs]
        try: self.log = json.load(open(SHUKA_LOG, encoding="utf-8"))
        except Exception: self.log = []
        import threading
        for i, A in enumerate(adbs):
            threading.Thread(target=self.loop, args=(i, A), daemon=True).start()

    def act(self, A, a, k, seed):
        rng = random.Random(seed); kind = a["action"]
        x, y = float(a.get("x", 0)) * k, float(a.get("y", 0)) * k
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

    def loop(self, i, A):
        inf = self.info[i]; same = 0; last_sig = None
        while True:
            if not self.run[i]:
                time.sleep(1); continue
            try:
                if not A.size: A.connect()
                data, off, w, h = A.raw_frame()
                k = turn_from_rgba(data, off, w, h)
                if k:   # our turn in a battle
                    inf["msg"] = f"バトル：{ORD.get(k)} の番"
                    time.sleep(random.uniform(.5, 3))
                    me, ally = ask_claude_battle(png_small(data, off, w, h, 2), w // 2, h // 2, ORD.get(k))
                    if me:
                        cx, cy = me[0] * 2, me[1] * 2; tx, ty = ally[0] * 2, ally[1] * 2
                        ang = math.degrees(math.atan2(ty - cy, tx - cx)) + random.uniform(-2, 2)
                    else:      # could not read the field: shoot upwards somewhere
                        cx, cy = w / 2, h * .55; ang = random.uniform(-150, -30)
                    A.shoot(ang, cx, cy, 1200, 120, True, "auto", random.random())
                    inf["steps"] += 1; inf["hist"].append(f"バトルで{ang:.0f}°に打った")
                    time.sleep(random.uniform(6, 9)); continue
                if self.sh._panel(data, off, w, h):          # battle, not our turn
                    inf["msg"] = "バトル：待ち"; time.sleep(random.uniform(1.5, 2.5)); continue
                sig = screen_sig(data, off, w, h)
                if sum(_gray_grid(data, off, w, h, 0, 1, 6, 10)) / 60 < 18:
                    inf["msg"] = "読み込み中"; time.sleep(random.uniform(1, 2)); continue
                same = same + 1 if last_sig and sig_dist(sig, last_sig) < .02 else 0; last_sig = sig
                if same >= 15:
                    self.run[i] = False; inf["msg"] = "止めました：同じ画面から進めません"; print(f"書庫自動 {A.serial}: 止めました（同じ画面のまま）"); continue
                if inf.get("_scroll"):
                    inf["_scroll"] = None
                # a learned fixed action for this look -> do it (no AI). If it did not move the screen, the AI takes over.
                ctx = "after" if inf["clears"] else "first"
                L = min((L for L in self.sh.learn if L.get("act") and L["act"].get("ctx", ctx) == ctx), key=lambda L: sig_dist(sig, L["sig"]), default=None)
                if L and sig_dist(sig, L["sig"]) < .05 and same < 2:
                    a = dict(L["act"]); a.setdefault("state", L.get("state")); a.setdefault("why", L.get("note", "")); how = "覚えた操作"; kk = 2
                else:
                    if L and sig_dist(sig, L["sig"]) < .05 and same >= 3:      # the learned action keeps failing here: forget it
                        with self.sh.lock: L.pop("act", None); self.sh.save()
                    hist = [f"この周回で書庫のクエストをクリアした数: {int(inf['clears'])}"] + inf["hist"] + ([f"※ 同じ画面が{same}回続いている。さっきと違う操作を試すこと"] if same >= 2 else []) \
                        + (["※ 書庫の一覧は一番下までスクロール済み（これ以上下はない）"] if inf.get("bottom") else [])
                    a, err = ask_claude_action(png_small(data, off, w, h, 2), w // 2, h // 2, hist)
                    with self.sh.lock: self.sh.ai_calls += 1
                    if not a:
                        inf["msg"] = "AI：" + err; time.sleep(8); continue
                    how = "AI"; kk = 2
                    # remember it when this screen always gets the same press. Screens whose press depends on the quest
                    # (which quest in the list, the deck, the filter steps) are always judged fresh. Home is remembered twice:
                    # once for the first visit (tap) and once for after a clear (long press).
                    if a.get("fixed") and a["action"] in ("tap", "longpress") and a.get("state") not in ("shuka_list", "deck_select", "filter", "battle", "other", "loading"):
                        with self.sh.lock:
                            self.sh.add(a.get("state", "other"), a.get("why", ""), sig, png_small(data, off, w, h, 6))
                            self.sh.learn[-1]["act"] = dict({x: a.get(x) for x in ("action", "x", "y", "x2", "y2", "state", "why")}, ctx=ctx); self.sh.save()
                st = a.get("state", "?"); why = a.get("why", "")
                if st != "shuka_list":
                    inf["bottom"] = False
                elif a["action"] == "stop" and not inf.get("bottom"):     # never give up on the list before having seen its bottom
                    a = {"action": "swipe", "state": st, "y": 1, "y2": 0}; why = "まだ一番下を見ていないのでスクロール"
                if st == "shuka_list" and a["action"] == "swipe" and float(a.get("y2", 0) or 0) <= float(a.get("y", 1) or 1):
                    # scrolling down the list: done here with long drags until it stops moving (no AI per drag)
                    inf["msg"] = "書庫の一覧を下までスクロール中"; print(f"書庫自動 {A.serial}: {inf['msg']}")
                    time.sleep(random.uniform(.6, 1.5))
                    for _ in range(10):
                        before = list_sig(*A.raw_frame())
                        x0 = w * random.uniform(.42, .6)
                        A.run("shell", "input", "swipe", *map(str, (round(x0), round(h * random.uniform(.80, .86)), round(x0 + random.uniform(-25, 25)),
                              round(h * random.uniform(.26, .32)), round(random.uniform(420, 650)))), timeout=15)
                        time.sleep(random.uniform(1.0, 1.8))
                        if sig_dist(list_sig(*A.raw_frame()), before) < .015:
                            break
                    inf["bottom"] = True; inf["steps"] += 1
                    inf["hist"] = (inf["hist"] + ["書庫の一覧を一番下までスクロールした"])[-14:]
                    continue
                if a.get("quest") and st in ("quest_detail", "helper_select", "deck_select", "loading"): inf["quest"] = str(a["quest"])[:40]
                inf["msg"] = f"{st}：{a['action']}（{how}）{why}"
                print(f"書庫自動 {A.serial}: {inf['msg']}")
                if a["action"] == "stop":
                    self.run[i] = False; inf["msg"] = "止めました：" + (why or st); continue
                if a["action"] == "wait":
                    time.sleep(random.uniform(1, 2.5)); continue
                # human-like pause before acting: usually short, sometimes a longer look (the AI already took a few seconds)
                time.sleep(random.uniform(.6, 2.5) if how == "AI" else (random.uniform(4, 9) if random.random() < .12 else random.uniform(1, 3.5)))
                self.act(A, a, kk, random.random())
                inf["steps"] += 1; inf["hist"] = (inf["hist"] + [f"{st}で{a['action']}（{why}）"])[-14:]
                if st == "result" and not inf.get("_res"):
                    inf["clears"] += 1
                    self.log.append({"t": time.strftime("%m/%d %H:%M"), "i": i + 1, "quest": inf.get("quest") or "（名前不明）", "orb": 1})
                    try: json.dump(self.log, open(SHUKA_LOG, "w", encoding="utf-8"), ensure_ascii=False)
                    except Exception: pass
                inf["_res"] = st == "result"
                time.sleep(random.uniform(.8, 1.6))
            except Exception as e:
                inf["msg"] = "エラー：" + str(e)[:80]; time.sleep(5)


SHUKA_PAGE = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>書庫周回</title><style>
body{margin:0;background:#0f1115;color:#e8eaef;font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
header{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid #2a2f3a}
h1{font-size:17px;margin:0}button,select,input{font:inherit;font-size:14px;border-radius:8px;border:1px solid #2a2f3a;background:#1c2029;color:#e8eaef;padding:8px 12px}
button.on{background:#1f5a3a;border-color:#3ddc84}.small{font-size:12px;color:#8b93a3}
main{display:flex;gap:16px;flex-wrap:wrap;padding:16px}.c{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:12px;width:300px;display:flex;flex-direction:column;gap:8px}
.c img{width:100%;border-radius:8px;background:#000;min-height:200px}.st{font-size:22px;font-weight:800;color:#ffcf3f}
</style></head><body><header><h1>書庫周回</h1>
<button id="run">判定を始める</button><span id="info" class="small"></span><span style="flex:1"></span>
<span id="keybox" class="small"></span></header><main id="m"></main>
<h2 style="font-size:15px;margin:8px 16px" id="lh"></h2><div id="LG" style="margin:0 16px;font-size:13px;line-height:1.8;max-height:260px;overflow:auto;background:#171a21;border:1px solid #2a2f3a;border-radius:10px;padding:8px 12px"></div>
<h2 style="font-size:15px;margin:8px 16px">覚えた画面（違っていたら直す・消す）</h2><main id="L"></main><script>
let S=null;const $=id=>document.getElementById(id);
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json();}
async function tick(){try{S=await (await fetch('/shuka/state')).json();render();}catch(e){$('info').textContent='ヘルパーにつながりません';}}
function render(){$('run').textContent=S.on?'判定を止める':'判定を始める';$('run').classList.toggle('on',S.on);
$('info').textContent=`覚えた画面 ${S.learned} ・ AIに聞いた回数 ${S.ai_calls}（実際の料金 約${S.yen.toFixed(2)}円）`;
if(S.key)$('keybox').textContent='APIキー：設定済み';
else if(!$('k')){$('keybox').innerHTML='APIキー未設定 <input id="k" type="password" placeholder="sk-ant-..." style="width:180px"> <button id="ks">保存</button>';
$('ks').onclick=async()=>{const r=await post('/shuka/key',{key:$('k').value.trim()});if(!r.ok)alert(r.error);else{$('keybox').textContent='APIキー：設定済み';tick();}};}
const opts=Object.entries(S.states).map(([k,v])=>`<option value="${k}">${k}：${v}</option>`).join('');
$('m').innerHTML=S.inst.map((s,i)=>`<div class="c"><div class="small">${i+1}画面目 ${s.serial}</div><div class="st">${s.state}</div>
<div class="small">${s.note||''} ${s.how?'・'+s.how:''}</div><img src="/shuka/thumb?i=${i}&t=${s.t}" alt="">
<button data-a="${i}" class="${S.auto&&S.auto[i].on?'on':''}">${S.auto&&S.auto[i].on?'自動を止める':'この画面を自動で回す'}</button>
<div class="small">${S.auto?S.auto[i].msg+' ・操作'+S.auto[i].steps+'回':''}</div>
<div class="small">違っていたら直す：</div><select data-i="${i}"><option value="">選ぶ…</option>${opts}</select></div>`).join('');
document.querySelectorAll('button[data-a]').forEach(b=>b.onclick=async()=>{const i=+b.dataset.a;await post('/shuka/auto',{i,on:!(S.auto&&S.auto[i].on)});tick();});
document.querySelectorAll('select[data-i]').forEach(x=>x.onchange=async()=>{if(x.value){await post('/shuka/fix',{i:+x.dataset.i,state:x.value});tick();}});
const lg=S.log||[];$('lh').textContent=`クリアしたクエスト ${lg.length}個 ・ オーブ ${lg.reduce((a,b)=>a+(b.orb||0),0)}個`;
$('LG').innerHTML=lg.length?lg.slice().reverse().map(l=>`${l.t}　${l.i}画面目　${l.quest}　+${l.orb}オーブ`).join('<br>'):'まだありません';
const sig=S.learn.map(l=>l.id+l.state).join();if(sig!==window._ls){window._ls=sig;
$('L').innerHTML=S.learn.map(l=>`<div class="c" style="width:170px"><div class="st" style="font-size:16px">${l.state}</div><div class="small">${l.note||''} ・${l.n}回</div>
<img src="/shuka/limg?id=${l.id}" alt="" style="min-height:0"><select data-l="${l.id}"><option value="">直す…</option>${opts}<option value="_delete">この見本を消す</option></select></div>`).join('');
document.querySelectorAll('select[data-l]').forEach(x=>x.onchange=async()=>{if(x.value){await post('/shuka/relabel',{id:x.dataset.l,state:x.value});window._ls='';tick();}});}}
$('run').onclick=async()=>{await post('/shuka/run',{on:!S.on});tick();};
tick();setInterval(()=>{if(!document.querySelector('select:focus'))tick();},3000);
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
    ThreadingHTTPServer(("127.0.0.1", LISTEN), make_handler(adbs)).serve_forever()


if __name__ == "__main__":
    main()
