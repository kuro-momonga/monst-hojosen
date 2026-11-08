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
HELPER_VER = 16
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
            raise RuntimeError((r.stderr or r.stdout).decode(errors="ignore").strip() or "adb error")
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

    def screencap(self):
        data = self.run("exec-out", "screencap", "-p", binary=True)
        if not data.startswith(b"\x89PNG"):  # some builds turn \n into \r\n
            data = data.replace(b"\r\n", b"\n")
        return data

    def shoot(self, angle_deg, cx, cy, ms, hold=400, wander=False, method="auto"):
        """Pull opposite to angle_deg (0 = right, clockwise +, screen y down) from (cx, cy).
        Picks the integer end point (pull length 0.3..0.42 of the width) whose direction is closest to the angle."""
        w, h = self.size
        a = math.radians(angle_deg)
        dx, dy = -math.cos(a), -math.sin(a)
        best = None
        for L in range(int(w * .30), int(w * .42)):
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
            # 寄り道: random total time, a smooth curve (Catmull-Rom) through random points, speed varying along it
            T = random.uniform(2, 4)
            k = random.randint(3, 6)
            P = [(sx, sy)] + [(random.uniform(.1, .9) * w, random.uniform(.15, .85) * h) for _ in range(k)] + [(ex, ey)]
            P = [P[0]] + P + [P[-1]]
            segs = len(P) - 3
            wts = [random.uniform(.4, 1.6) for _ in range(segs)]; tot = sum(wts)
            for j in range(segs):
                p0, p1, p2, p3 = P[j], P[j + 1], P[j + 2], P[j + 3]
                dur = T * wts[j] / tot
                n = max(3, round(dur / .008))
                for i in range(1, n + 1):
                    u = i / n; u = u * u * (3 - 2 * u)   # slow down / speed up around each point
                    u2, u3 = u * u, u * u * u
                    x = .5 * (2 * p1[0] + (-p0[0] + p2[0]) * u + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * u2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * u3)
                    y = .5 * (2 * p1[1] + (-p0[1] + p2[1]) * u + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * u2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * u3)
                    path.append((min(w - 5, max(5, x)), min(h - 5, max(5, y)), dur / n))
        else:
            n = max(6, round(ms / 1000 / .008))
            for i in range(1, n + 1):
                t = i / n; t = t * t * (3 - 2 * t)
                path.append((sx + (ex - sx) * t, sy + (ey - sy) * t, ms / 1000 / n))
        if wander:
            # settle like a hand: overshoot a little, then 2-3 small corrections onto the aim
            ox, oy = random.gauss(0, 9), random.gauss(0, 9)
            for k in range(random.randint(2, 3)):
                f = (.45 ** k) * random.choice((1, -1)) if k else 1
                tx, ty = ex + ox * f, ey + oy * f
                lx, ly = path[-1][0], path[-1][1]
                n = random.randint(12, 30)
                for i in range(1, n + 1):
                    u = i / n; u = u * u * (3 - 2 * u)
                    path.append((lx + (tx - lx) * u, ly + (ty - ly) * u, random.uniform(.006, .012)))
            lx, ly = path[-1][0], path[-1][1]
            for i in range(1, 16):
                u = i / 15; path.append((lx + (ex - lx) * u, ly + (ey - ly) * u, .01))
            # hand noise: tremor (8-12 Hz, ~2 px) + small random jitter + uneven frame timing + micro pauses
            f1, f2 = random.uniform(8, 12), random.uniform(3, 6)
            a1, a2 = random.uniform(1, 2.5), random.uniform(1, 3)
            ph = [random.uniform(0, 6.3) for _ in range(4)]
            t, out = 0.0, []
            fade_from = len(path) - 15
            for idx, (x, y, dt) in enumerate(path):
                t += dt
                g = 0 if idx >= fade_from else 1
                nx = a1 * math.sin(2 * math.pi * f1 * t + ph[0]) + a2 * math.sin(2 * math.pi * f2 * t + ph[1]) + random.gauss(0, .6)
                ny = a1 * math.sin(2 * math.pi * f1 * t + ph[2]) + a2 * math.sin(2 * math.pi * f2 * t + ph[3]) + random.gauss(0, .6)
                dt2 = dt * random.uniform(.75, 1.25)
                if g and random.random() < .004:
                    dt2 += random.uniform(.05, .2)   # tiny hesitation
                out.append((min(w - 5, max(5, x + g * nx)), min(h - 5, max(5, y + g * ny)), dt2))
            path = out
        path.append((ex, ey, hold / 1000))

        how, errs = None, []
        if self.touch() and method != "swipe":
            try:
                if method == "script":
                    self.send_touch_script(sx, sy, path[::4] + [path[-1]]); how = "script"
                else:
                    self.send_touch(sx, sy, path); how = "sendevent"
            except Exception as e:
                errs.append(f"sendevent: {e}")
        if how is None:
            self.run("shell", "input", "swipe", str(sx), str(sy), str(ex), str(ey), str(int(ms + hold)), timeout=30)
            how = "swipe"
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
        send((3, 57, 1), (3, 53, X), (3, 54, Y), (1, 330, 1), (0, 0, 0))
        for x, y, wait in path:
            due += wait
            X, Y = conv(x, y)
            send((3, 53, X), (3, 54, Y), (0, 0, 0))
            sl = t0 + due - time.perf_counter()
            if sl > 0: time.sleep(sl)
        # end inside the same stream (so nothing can overtake it): exact aim point, hold, release
        ex_, ey_, _ = path[-1]; X, Y = conv(ex_, ey_)
        send((3, 53, X), (3, 54, Y), (0, 0, 0)); time.sleep(.12)
        send((3, 53, X), (3, 54, Y), (0, 0, 0)); time.sleep(.12)
        send((3, 57, -1), (1, 330, 0), (0, 0, 0))
        self._sent_all = True
        for _ in range(4):          # harmless empty SYN reports: push the tail through any buffering
            send(*[(0, 0, 0)] * 100); time.sleep(.05)
        time.sleep(1.0)            # let adb deliver the tail before closing (closing early dropped the release)
        p.stdin.close()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        err = (p.stderr.read() or b"").decode(errors="ignore").strip()
        print("dd:", err.replace("\n", " | ")[:200])
        if any(k in err for k in ("nvalid", "denied", "not found", "rror")):
            raise RuntimeError(err[:200])

    def send_touch_script(self, sx, sy, path):
        d = self._touch["dev"]; conv = self.conv_fn()
        ev = lambda a, b, c: f"sendevent {d} {a} {b} {c}"
        X, Y = conv(sx, sy)
        cmds = [ev(3, 57, 1), ev(3, 53, X), ev(3, 54, Y), ev(1, 330, 1), ev(0, 0, 0)]
        pend = 0.0
        for x, y, p in path:
            X, Y = conv(x, y)
            cmds += [ev(3, 53, X), ev(3, 54, Y), ev(0, 0, 0)]
            pend += p - .015   # each sendevent line costs a little time itself
            if pend > .02:
                cmds.append(f"sleep {pend:.2f}"); pend = 0.0
        cmds += [ev(3, 57, -1), ev(1, 330, 0), ev(0, 0, 0)]
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


def make_handler(adb):
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
                    return self.reply(200, {"ok": True, "w": w, "h": h, "serial": adb.serial, "ver": HELPER_VER, "touch": adb.touch(), "rot": adb.rot, "natural": adb.natural})
                if self.path.startswith("/screen"):
                    if not adb.size:
                        adb.connect()
                    return self.reply(200, adb.screencap(), "image/png")
                return self.reply(404, {"ok": False, "error": "not found"})
            except Exception as e:
                return self.reply(500, {"ok": False, "error": str(e)})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                q = json.loads(self.rfile.read(n) or b"{}")
                if self.path.startswith("/shoot"):
                    if not adb.size:
                        adb.connect()
                    adb.rot_override = q.get("rot")
                    w, h = adb.size
                    cx = q.get("cx", w / 2)
                    cy = q.get("cy", h * .45)
                    r = adb.shoot(float(q["angle"]), cx, cy, float(q.get("ms", 1200)), float(q.get("hold", 400)), bool(q.get("wander")), q.get("method", "auto"))
                    print(f"shoot {q['angle']:.2f}deg  {r}")
                    return self.reply(200, {"ok": True, **r})
                return self.reply(404, {"ok": False, "error": "not found"})
            except Exception as e:
                return self.reply(500, {"ok": False, "error": str(e)})

        def log_message(self, *a):
            pass

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="5555", help="BlueStacks の ADB ポート（設定に表示される番号）")
    ap.add_argument("--adb", default=None, help="adb.exe の場所（省略すると自動で探す）")
    a = ap.parse_args()
    exe = a.adb or find_adb()
    if not exe:
        print("adb が見つかりません。--adb で HD-Adb.exe の場所を指定してください"); sys.exit(1)
    adb = Adb(exe, f"127.0.0.1:{a.port}")
    try:
        w, h = adb.connect()
        print(f"BlueStacks に接続しました（{adb.serial}、画面 {w}x{h}）")
    except Exception as e:
        print(f"まだ BlueStacks に接続できていません: {e}\n（BlueStacks を起動して ADB をオンにしてください。ツールから接続し直せます）")
    print(f"ヘルパー起動中: http://127.0.0.1:{LISTEN}  （このウィンドウを閉じると止まります）")
    ThreadingHTTPServer(("127.0.0.1", LISTEN), make_handler(adb)).serve_forever()


if __name__ == "__main__":
    main()
