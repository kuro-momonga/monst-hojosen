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
HELPER_VER = 40
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
        return turn_from_rgba(data, off, w, h)

    def tap(self, x, y, seed=None):
        """a quick click-like tap (finger down ~60-90 ms). One binary stream, so it is never a long press
        (separate sendevent calls took long enough to open the character's info instead of the strike shot)"""
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
        self.unstick()
        p = subprocess.Popen([self.exe, "-s", self.serial, "exec-in", f"dd of={d} ibs=4096 obs={self._evsize}"], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            p.stdin.write(pk([(0, 0, 0)] * 20)); p.stdin.flush(); time.sleep(.15)   # let dd start first
            p.stdin.write(pk(self.E_down(X, Y))); p.stdin.flush()
            time.sleep(rng.uniform(.06, .09))
            p.stdin.write(pk(self.E_up()) + pk([(0, 0, 0)] * 100)); p.stdin.flush()
            time.sleep(.15)
            p.stdin.close(); p.wait(timeout=1.5)
        except Exception:
            try: p.kill()
            except Exception: pass
        # make sure the finger is up even if the stream's tail got stuck somewhere
        self.run("shell", "; ".join(f"sendevent {d} {a} {b_} {c}" for a, b_, c in self.E_up()) + "; true", timeout=10)
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
        if wander:   # same moves, played fast: the whole aiming takes about 1.3-1.9 s (shot ~2-3 s after the button)
            T = sum(dt for _, _, dt in path) or 1
            K = rng.uniform(1.3, 1.9) / T
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
            prev, st["raw"] = st["raw"], k
            if k != prev:          # needs the same reading twice in a row
                time.sleep(.3); continue
            if k == 0:
                st["zeros"] += 1
            else:
                fire = st["zeros"] >= 2 or k != st["fired"]
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
            time.sleep(.3)

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
                                time.sleep(random.Random(seed).uniform(.3, .45))   # like the strike shot: just armed, no waiting
                            if ss:
                                A.tap(float(ss[0]), float(ss[1]), seed)
                                time.sleep(random.Random(seed).uniform(.35, .55))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None, help="BlueStacks の ADB ポート（複数の画面に同時に打つならカンマ区切り: 5575,5585,5595）")
    ap.add_argument("--adb", default=None, help="adb.exe の場所（省略すると自動で探す）")
    a = ap.parse_args()
    exe = a.adb or find_adb()
    if not exe:
        print("adb が見つかりません。--adb で HD-Adb.exe の場所を指定してください"); sys.exit(1)
    port = a.port
    if not port:   # ask here (cmd.exe garbles long Japanese prompts in .bat files)
        last_f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ports.txt")
        last = open(last_f).read().strip() if os.path.exists(last_f) else "5555"
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
    ThreadingHTTPServer(("127.0.0.1", LISTEN), make_handler(adbs)).serve_forever()


if __name__ == "__main__":
    main()
