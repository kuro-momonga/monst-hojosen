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
import argparse, json, math, os, random, shutil, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = 8765
HELPER_VER = 6
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
        self.size = (w, h)
        return self.size

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
        steps = max(4, round(ms / 250))
        # path: list of (x, y, pause_after_s). Only the release point decides the shot.
        base = math.atan2(ey - sy, ex - sx); L0 = math.hypot(ex - sx, ey - sy)
        def at(ang, L):
            return (min(w - 5, max(5, sx + math.cos(ang) * L)), min(h - 5, max(5, sy + math.sin(ang) * L)))
        path = []
        cur_a, cur_L = base + random.uniform(-.3, .3), L0 * random.uniform(.3, .6)
        if wander:
            # 寄り道: swing up to ±180° from the real aim for about 3 s, sometimes fast, sometimes slow
            path.append((*at(cur_a, cur_L), .05))
            T, used = random.uniform(2.5, 3.5), 0.0
            while used < T:
                ta = base + math.radians(random.uniform(-180, 180)); tL = L0 * random.uniform(.4, 1.15)
                dur = min(T - used, random.choice([random.uniform(.08, .25), random.uniform(.3, .6), random.uniform(.7, 1.2)]))
                n = max(2, round(dur / .04))
                for i in range(1, n + 1):
                    t = i / n; t = t * t * (3 - 2 * t)
                    path.append((*at(cur_a + (ta - cur_a) * t, cur_L + (tL - cur_L) * t), dur / n))
                if random.random() < .35:
                    path.append((*at(ta, tL), random.uniform(.1, .5)))   # hesitate
                cur_a, cur_L = ta, tL; used += dur
        else:
            cur_a, cur_L = base, 0
        n = max(6, round(ms / 1000 / .04))
        for i in range(1, n + 1):
            t = i / n; t = t * t * (3 - 2 * t)
            da = (base - cur_a + math.pi) % (2 * math.pi) - math.pi
            path.append((*at(cur_a + da * t, cur_L + (L0 - cur_L) * t), ms / 1000 / n))
        path.append((ex, ey, hold / 1000))

        how, errs = None, []
        if self.touch() and method != "swipe":
            try:
                self.send_touch(sx, sy, path); how = "sendevent"
            except Exception as e:
                errs.append(f"sendevent: {e}")
        if how is None and wander and method != "swipe":
            try:
                cmds = [f"input motionevent DOWN {sx} {sy}"] + [f"input motionevent MOVE {round(x)} {round(y)}" for x, y, _ in path[::6]] + [f"input motionevent MOVE {ex} {ey}", f"sleep {hold / 1000:.2f}", f"input motionevent UP {ex} {ey}"]
                out = self.run("shell", "; ".join(cmds), timeout=40)
                if "Error" in out or "Unknown" in out:
                    raise RuntimeError(out.strip()[:200])
                how = "motionevent"
            except Exception as e:
                errs.append(f"motionevent: {e}")
        if how is None:
            self.run("shell", "input", "swipe", str(sx), str(sy), str(ex), str(ey), str(int(ms + hold)), timeout=30)
            how = "swipe"
        return {"from": [sx, sy], "to": [ex, ey], "err_deg": round(err, 4), "how": how, "notes": errs}

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
        t = self._touch; w, h = self.size; d = t["dev"]
        (x0, x1), (y0, y1) = t["x"], t["y"]
        swap = (x1 - x0 > y1 - y0) != (w > h)   # touch panel in the other orientation
        def conv(x, y):
            if swap:
                x, y = y, w - x
                return round(x0 + x / (h - 1) * (x1 - x0)), round(y0 + y / (w - 1) * (y1 - y0))
            return round(x0 + x / (w - 1) * (x1 - x0)), round(y0 + y / (h - 1) * (y1 - y0))
        ev = lambda a, b, c: f"sendevent {d} {a} {b} {c}"
        X, Y = conv(sx, sy)
        cmds = [ev(3, 57, 1), ev(3, 53, X), ev(3, 54, Y), ev(1, 330, 1), ev(0, 0, 0)]
        pend = 0.0
        for x, y, p in path:
            X, Y = conv(x, y)
            cmds += [ev(3, 53, X), ev(3, 54, Y), ev(0, 0, 0)]
            pend += p - .03   # each sendevent line costs a little time itself
            if pend > .02:
                cmds.append(f"sleep {pend:.2f}"); pend = 0.0
        cmds += [ev(3, 57, -1), ev(1, 330, 0), ev(0, 0, 0)]
        out = self.run("shell", "; ".join(cmds), timeout=60)
        if "rror" in out or "denied" in out.lower():
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
                    return self.reply(200, {"ok": True, "w": w, "h": h, "serial": adb.serial, "ver": HELPER_VER, "touch": adb.touch()})
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
