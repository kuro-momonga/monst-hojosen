// Phone status page for the 書庫 auto-farm.
// The helper on the PC POSTs a small status JSON to /st/<token> every ~30 s; the phone opens /st/<token>.
// Everything else is the static site (public/).
import { DurableObject } from "cloudflare:workers";

export class Status extends DurableObject {
  async fetch(req) {
    if (req.method === "POST") {
      const body = await req.text();
      if (body.length > 400000) return new Response("too big", { status: 413 });
      await this.ctx.storage.put("s", body);
      await this.ctx.storage.put("at", Date.now());
      return new Response("ok");
    }
    const s = (await this.ctx.storage.get("s")) || "{}";
    const at = (await this.ctx.storage.get("at")) || 0;
    return new Response(JSON.stringify({ at, s: JSON.parse(s) }), { headers: { "content-type": "application/json", "cache-control": "no-store" } });
  }
}

const PAGE = `<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><title>書庫周回</title><style>
:root{--bg:#0e1014;--card:#171a21;--line:#262b36;--tx:#e8eaef;--mut:#8b93a3;--go:#3ddc84;--warn:#ffcf3f;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,"Hiragino Sans",sans-serif;padding:12px 12px 40px}
.top{display:flex;justify-content:space-between;align-items:baseline;margin:4px 2px 12px}.top h1{font-size:18px;margin:0}
.big{display:flex;gap:10px;margin-bottom:12px}.big div{flex:1;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;text-align:center}
.big b{display:block;font-size:26px;color:var(--warn)}.big span{font-size:12px;color:var(--mut)}
.c{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;margin-bottom:10px;display:flex;gap:10px}
.c img{width:84px;aspect-ratio:9/16;object-fit:cover;border-radius:8px;background:#000;flex:none}
.r{flex:1;min-width:0;display:flex;flex-direction:column;gap:5px}.nm{display:flex;justify-content:space-between;font-weight:700}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;background:#2a2f3a;color:var(--mut);font-weight:400}
.pill.run{background:#173d2a;color:var(--go)}.pill.rest{background:#3d3517;color:var(--warn)}.pill.stopd{background:#3d1a1d;color:var(--bad)}
.kv{font-size:12px;color:var(--mut)}.kv b{color:var(--tx)}.msg{font-size:12px;line-height:1.45;color:#cfd4dd;word-break:break-all}
.old{background:#3d1a1d;color:#ffd0d0;border-radius:10px;padding:10px;font-size:13px;margin-bottom:12px;display:none}
h2{font-size:14px;margin:16px 2px 8px}.lg{font-size:12px;line-height:1.9;color:#cfd4dd}.small{font-size:11px;color:var(--mut)}
</style></head><body>
<div class="top"><h1>書庫周回</h1><span class="small" id="at"></span></div>
<div class="old" id="old"></div>
<div class="big"><div><b id="orbs">-</b><span>きょうのオーブ</span></div><div><b id="clr">-</b><span>クリア（合計）</span></div><div><b id="yen" style="font-size:18px;line-height:31px">-</b><span>AI料金</span></div></div>
<div id="m"></div><h2>最近のクリア</h2><div class="lg" id="lg"></div>
<script>
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function pill(a){if(!a.on)return (a.msg||'').startsWith('止めました')?['stopd','止まりました']:['','停止中'];if((a.msg||'').startsWith('休憩'))return['rest','休憩中'];return['run','動いています'];}
async function tick(){try{const r=await (await fetch(location.pathname.replace(/\\/$/,'')+'/data?'+Date.now())).json();const s=r.s||{};
 const age=Math.round((Date.now()-r.at)/1000);
 document.getElementById('at').textContent=r.at?new Date(r.at).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit',second:'2-digit'})+' 更新':'';
 const old=document.getElementById('old');
 if(!r.at){old.style.display='block';old.textContent='まだ届いていません。PCでヘルパーを起動し直してください。';}
 else if(age>180){old.style.display='block';old.textContent='PCから'+Math.round(age/60)+'分届いていません（ヘルパーが止まった・PCがスリープ・ネットが切れた など）';}
 else old.style.display='none';
 document.getElementById('orbs').textContent=s.orbs_today??'-';document.getElementById('clr').textContent=s.clears??'-';
 document.getElementById('yen').textContent=s.yen!=null?('約'+Number(s.yen).toFixed(2)+'円'):'-';
 document.getElementById('m').innerHTML=(s.inst||[]).map((a,i)=>{const[pc,pt]=pill(a);
  return '<div class="c">'+(a.png?'<img src="data:image/png;base64,'+a.png+'">':'')+'<div class="r"><div class="nm"><span>'+(i+1)+'画面目</span><span class="pill '+pc+'">'+pt+'</span></div>'+
  '<div class="kv">難易度 <b>'+esc(a.done?'全部おわり':a.diff)+'</b>　クリア <b>'+(a.clears||0)+'</b></div><div class="msg">'+esc(a.msg)+'</div></div></div>';}).join('');
 document.getElementById('lg').innerHTML=(s.log||[]).slice().reverse().map(l=>esc(l.t)+'　'+l.i+'画面目　'+esc(l.quest)).join('<br>')||'<span class="small">まだありません</span>';
}catch(e){document.getElementById('at').textContent='読み込めません';}}
tick();setInterval(tick,15000);
</script></body></html>`;

export default {
  async fetch(req, env) {
    const u = new URL(req.url);
    const m = u.pathname.match(/^\/st\/([A-Za-z0-9_-]{16,64})(\/data)?\/?$/);
    if (m) {
      const stub = env.ST.get(env.ST.idFromName(m[1]));
      if (req.method === "POST") return stub.fetch(new Request("https://do/", { method: "POST", body: await req.text() }));
      if (m[2]) return stub.fetch(new Request("https://do/"));
      return new Response(PAGE, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
    }
    return env.ASSETS.fetch(req);
  },
};
