from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import sqlite3, os, secrets
from datetime import datetime, timedelta

APP_VERSION = "immediate-v4"  # ← ページと /_version で確認用

# --- Basic 認証 ---
APP_USER = os.getenv("APP_USER", "staff")
APP_PASSWORD = os.getenv("APP_PASSWORD", "change-me")
security = HTTPBasic()
def verify(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username or "", APP_USER)
    ok_pass = secrets.compare_digest(credentials.password or "", APP_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic"})

# --- JST ---
try:
    from zoneinfo import ZoneInfo
    TZ_TOKYO = ZoneInfo("Asia/Tokyo")
except Exception:
    TZ_TOKYO = None
def now_jst(): return datetime.now(TZ_TOKYO) if TZ_TOKYO else datetime.now()
def today_key(): return now_jst().strftime("%Y-%m-%d")

DB_PATH = os.path.join(os.path.dirname(__file__), "app.db")

# ===== DB 初期化・マイグレーション =====
def init_db():
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")

    # rooms
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rooms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        capacity INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('available','occupied','cleaning','disabled')),
        eta_at TEXT,
        kind TEXT DEFAULT 'private',   -- 'private' | 'hall'
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    # requests（待機/heading は廃止）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        headcount INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('in_room','completed','canceled')),
        assigned_room_id INTEGER REFERENCES rooms(id),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    # 追加カラム
    def add_col_if_missing(table, col, decl):
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        if col not in cols: cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    add_col_if_missing("requests", "day_key", "TEXT")
    add_col_if_missing("requests", "seq", "INTEGER")
    add_col_if_missing("requests", "target_area", "TEXT DEFAULT 'private'")    # private / reino_hall / kami_hall
    add_col_if_missing("requests", "allocated_seats", "INTEGER")               # 座敷の消費座席（2 or 4）

    # 万一旧スキーマ（pending/heading）が残っていたら安全に再生成して吸収
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='requests'")
    sql = (cur.fetchone() or [""])[0] or ""
    if "pending" in sql or "heading" in sql:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("""
        CREATE TABLE requests_new(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            headcount INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('in_room','completed','canceled')),
            assigned_room_id INTEGER REFERENCES rooms(id),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            day_key TEXT,
            seq INTEGER,
            target_area TEXT DEFAULT 'private',
            allocated_seats INTEGER
        )
        """)
        cur.execute("""
            INSERT INTO requests_new(id, headcount, status, assigned_room_id, created_at, updated_at,
                                     day_key, seq, target_area, allocated_seats)
            SELECT id,
                   headcount,
                   CASE status WHEN 'in_room' THEN 'in_room'
                               WHEN 'completed' THEN 'completed'
                               WHEN 'canceled' THEN 'canceled'
                               ELSE 'canceled' END,
                   assigned_room_id, created_at, updated_at,
                   day_key, seq,
                   COALESCE(target_area,'private'),
                   allocated_seats
              FROM requests
        """)
        cur.execute("DROP TABLE requests")
        cur.execute("ALTER TABLE requests_new RENAME TO requests")
        cur.execute("COMMIT")

    # 初期部屋（個室8 + 座敷2）
    cur.execute("SELECT COUNT(*) FROM rooms")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO rooms(name,capacity,status,kind) VALUES(?,?, 'available','private')",
            [("１号室",4), ("２号室",4), ("３号室",4), ("５号室",2),
             ("６号室",4), ("７号室",6), ("８号室",4), ("１０号室",4)]
        )
        cur.executemany(
            "INSERT INTO rooms(name,capacity,status,kind) VALUES(?,?,'available','hall')",
            [("霊の湯2階座敷", 20), ("神の湯2階座敷", 70)]
        )
    else:
        # 命名揺れ修正
        cur.execute("UPDATE rooms SET name='神の湯2階座敷' WHERE name IN ('神の湯2階','神の湯2回座敷')")
        # 座敷が無ければ追加
        def ensure_room(name, cap):
            if not cur.execute("SELECT 1 FROM rooms WHERE name=?", (name,)).fetchone():
                cur.execute("INSERT INTO rooms(name,capacity,status,kind) VALUES(?,?,'available','hall')",(name,cap))
        ensure_room("霊の湯2階座敷", 20)
        ensure_room("神の湯2階座敷", 70)

    con.commit(); con.close()
init_db()

# ===== ユーティリティ =====
def seats_needed_for_group(headcount:int)->int:
    if headcount <= 0: raise HTTPException(400, "人数は1以上にしてください")
    if headcount <= 2: return 2
    if headcount <= 4: return 4
    raise HTTPException(409, "座敷の1グループは4名までにしてください（分割依頼で対応可）")

def hall_seats_used(cur, room_id:int)->int:
    cur.execute("SELECT COALESCE(SUM(allocated_seats),0) FROM requests WHERE assigned_room_id=? AND status='in_room'", (room_id,))
    return int(cur.fetchone()[0] or 0)
def hall_people_used(cur, room_id:int)->int:
    cur.execute("SELECT COALESCE(SUM(headcount),0) FROM requests WHERE assigned_room_id=? AND status='in_room'", (room_id,))
    return int(cur.fetchone()[0] or 0)

# ===== FastAPI =====
app = FastAPI(dependencies=[Depends(verify)])
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ===== HTML（待機/heading なし・即割当） =====
HTML = f"""
<!doctype html>
<meta charset="utf-8">
<title>道後温泉｜空き部屋・割当（即割当 v4）</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root{{ --radius:14px; }}
  body{{font-family:system-ui,-apple-system,Segoe UI,Roboto; margin:0; padding:16px;}}
  h1{{font-size:20px;margin:0 0 12px;}}
  h2{{font-size:18px;margin:14px 0 8px;}}
  h3{{font-size:16px;margin:12px 0 6px;}}
  .grid{{display:grid; gap:16px; grid-template-columns:1fr 1fr;}}
  .card{{border:1px solid #ddd; border-radius:14px; padding:12px;}}
  .row{{display:flex; gap:10px; align-items:center; flex-wrap:wrap}}
  .btn{{padding:10px 14px; border-radius:12px; border:1px solid #222; background:#222; color:#fff; cursor:pointer}}
  .btn-outline{{background:#fff; color:#222}}
  .num{{font-size:20px;padding:8px 10px;width:90px;text-align:center;border-radius:10px;border:1px solid #ccc;}}
  .list{{margin:8px 0; padding:0; list-style:none}}
  .item{{padding:8px 0; border-bottom:1px solid #eee; display:flex; justify-content:space-between; align-items:center; gap:8px}}
  @media (max-width:900px){{.grid{{grid-template-columns:1fr}}}}
  .section{{margin-top:12px;padding-top:8px;border-top:2px solid #eee}}
  .badge{{font-size:12px; padding:2px 6px; border-radius:999px; background:#eee}}
</style>

<h1>道後温泉｜空き部屋・割当（即割当 v4）</h1>

<div class="grid">
  <!-- 左：受付（即割当） -->
  <div class="card">
    <h2>受付：その場で割当</h2>

    <div class="section">
      <h3>霊の湯3階個室</h3>
      <div id="privateCreate"></div>
    </div>

    <div class="section">
      <h3>霊の湯2階座敷</h3>
      <div class="row">
        <input id="hc_reino" type="number" min="1" value="1" class="num">
        <button class="btn" onclick="quickAssign('reino_hall')">割当</button>
        <span id="reinoInfo"></span>
      </div>
    </div>

    <div class="section">
      <h3>神の湯2階座敷</h3>
      <div class="row">
        <input id="hc_kami" type="number" min="1" value="1" class="num">
        <button class="btn" onclick="quickAssign('kami_hall')">割当</button>
        <span id="kamiInfo"></span>
      </div>
    </div>
  </div>

  <!-- 右：状態 -->
  <div class="card">
    <h2>休憩室：グループ表示</h2>
    <div class="section">
      <h3>霊の湯3階個室</h3>
      <div id="rooms_private"></div>
    </div>
    <div class="section">
      <h3>霊の湯2階座敷</h3>
      <div id="rooms_reino"></div>
    </div>
    <div class="section">
      <h3>神の湯2階座敷</h3>
      <div id="rooms_kami"></div>
    </div>
  </div>
</div>

<script>
async function fetchJSON(u){{ const r=await fetch(u); if(!r.ok) throw new Error(await r.text()); return r.json(); }}
async function post(u, obj){{ const r=await fetch(u,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(obj)}}); if(!r.ok) throw new Error(await r.text()); return r.json(); }}

function hhmm(iso){{ if(!iso) return ''; const d=new Date(iso); if(isNaN(d)) return ''; const p=n=>String(n).padStart(2,'0'); return p(d.getHours())+':'+p(d.getMinutes()); }}

async function quickAssign(area){{
  const n = parseInt((area==='reino_hall'?document.getElementById('hc_reino').value:document.getElementById('hc_kami').value)||'1');
  try{{ await post('/api/quick_assign',{{targetArea:area, headcount:n}}); await load(); }}
  catch(e){{ alert(e.message); }}
}}

function buildPrivateCreate(rooms){{
  const wrap = document.createElement('div');
  const row = document.createElement('div'); row.className='row';
  const input = document.createElement('input'); input.type='number'; input.min='1'; input.value='1'; input.className='num';
  const sel = document.createElement('select');
  rooms.filter(r=>r.status==='available').forEach(r=>{{
    const o = document.createElement('option'); o.value = r.id; o.textContent = `${{r.name}}（${{r.capacity}}人）`; sel.appendChild(o);
  }});
  const btn = document.createElement('button'); btn.className='btn'; btn.textContent='この部屋に割当';
  btn.onclick = async ()=>{{
    try{{
      const headcount = parseInt(input.value||'1');
      const roomId = parseInt(sel.value);
      await post('/api/quick_assign',{{targetArea:'private', headcount, roomId}});
      await load();
    }}catch(e){{ alert(e.message); }}
  }};
  const info = document.createElement('span'); info.innerHTML = rooms.filter(r=>r.status==='available').length ? '' : '<span class="badge">空室なし</span>';

  row.appendChild(input); row.appendChild(sel); row.appendChild(btn); row.appendChild(info);
  wrap.appendChild(row);
  return wrap;
}}

function roomStatusLabel(s){{
  switch(s){{
    case 'available': return '空室';
    case 'occupied':  return '使用中';
    case 'cleaning':  return '清掃中';
    case 'disabled':  return '使用停止中';
    default:          return s;
  }}
}}

function buildCardPrivate(r){{
  const card = document.createElement('div'); card.className='card';
  card.innerHTML = `<b>${{r.name}}</b>（目安${{r.capacity}}人） <span class="badge">${{roomStatusLabel(r.status)}}</span>`;
  const inner = document.createElement('div'); card.appendChild(inner);

  if(r.status==='occupied' && r.currentRequestId){{
    inner.innerHTML += `<div>#${{r.currentSeq}} / 現在：${{r.currentHeadcount}}人</div>`;
    const row = document.createElement('div'); row.className='row'; row.style.marginTop='6px';
    const t = document.createElement('input'); t.type='time'; t.value = hhmm(r.eta_at)||'';
    const save = document.createElement('button'); save.className='btn btn-outline'; save.textContent='空き予定を保存';
    save.onclick = ()=>post('/api/rooms/eta',{{roomId:r.id, hhmm:t.value}}).then(load).catch(e=>alert(e.message));
    const out = document.createElement('button'); out.className='btn'; out.textContent='退室';
    out.onclick = ()=>post('/api/checkout',{{requestId:r.currentRequestId}}).then(load).catch(e=>alert(e.message));
    row.appendChild(document.createTextNode('空き予定：')); row.appendChild(t); row.appendChild(save); row.appendChild(out);
    inner.appendChild(row);
  }}
  return card;
}}

function buildCardHall(r){{
  const card = document.createElement('div'); card.className='card';
  const remain = Math.max(0, r.capacity - r.hall_seats);
  card.innerHTML = `<b>${{r.name}}</b>（座席${{r.capacity}}） <span class="badge">${{roomStatusLabel(r.status)}}</span>
                    <div>使用中：${{r.hall_people}}人 / 消費座席：${{r.hall_seats}} / 残り座席：${{remain}}</div>`;
  if(r.hall_list && r.hall_list.length){{
    const ul = document.createElement('ul'); ul.className='list';
    r.hall_list.forEach(x=>{{
      const li = document.createElement('li'); li.className='item';
      li.innerHTML = `#${{x.seq}} / ${{x.headcount}}名 / 消費座席:${{x.allocated_seats}}`;
      const box = document.createElement('span');
      const out = document.createElement('button'); out.className='btn btn-outline'; out.textContent='退室';
      out.onclick = ()=>post('/api/checkout',{{requestId:x.id}}).then(load).catch(e=>alert(e.message));
      box.appendChild(out); li.appendChild(box); ul.appendChild(li);
    }});
    card.appendChild(ul);
  }}
  return card;
}}

async function load(){{
  const s = await fetchJSON('/api/snapshot_immediate');

  // 受付（個室）
  const pc = document.getElementById('privateCreate'); pc.innerHTML='';
  pc.appendChild(buildPrivateCreate(s.rooms.private));

  // 受付（座敷）残情報
  document.getElementById('reinoInfo').innerHTML = `残り座席：<b>${{Math.max(0, s.summary.reino_remain)}}</b>`;
  document.getElementById('kamiInfo').innerHTML  = `残り座席：<b>${{Math.max(0, s.summary.kami_remain)}}</b>`;

  // 右カラム：部屋
  const rp = document.getElementById('rooms_private'); rp.innerHTML='';
  s.rooms.private.forEach(r=> rp.appendChild(buildCardPrivate(r)));

  const rr = document.getElementById('rooms_reino'); rr.innerHTML='';
  s.rooms.reino_hall.forEach(r=> rr.appendChild(buildCardHall(r)));

  const rk = document.getElementById('rooms_kami'); rk.innerHTML='';
  s.rooms.kami_hall.forEach(r=> rk.appendChild(buildCardHall(r)));
}}

load();
setInterval(load, 3000);
</script>
"""

@app.get("/", response_class=HTMLResponse)
def home(): return HTML

@app.get("/_version")
def version(): return JSONResponse({"version": APP_VERSION})

# ===== データ整形 =====
def fetch_rooms_grouped():
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute("""
      SELECT
        r.*,
        (SELECT id FROM requests q WHERE q.assigned_room_id=r.id AND q.status='in_room' LIMIT 1)        AS currentRequestId,
        (SELECT headcount FROM requests q WHERE q.assigned_room_id=r.id AND q.status='in_room' LIMIT 1) AS currentHeadcount,
        (SELECT seq FROM requests q WHERE q.assigned_room_id=r.id AND q.status='in_room' LIMIT 1)       AS currentSeq
      FROM rooms r
      ORDER BY r.id
    """).fetchall()

    groups = {"private": [], "reino_hall": [], "kami_hall": []}
    for r in rows:
        d = dict(r)
        if d["kind"] == "hall":
            d["hall_seats"]  = hall_seats_used(cur, d["id"])
            d["hall_people"] = hall_people_used(cur, d["id"])
            lst = cur.execute("""
                SELECT id, seq, headcount, COALESCE(allocated_seats,0) AS allocated_seats
                  FROM requests
                 WHERE assigned_room_id=? AND status='in_room'
                 ORDER BY updated_at DESC
            """,(d["id"],)).fetchall()
            d["hall_list"] = [dict(x) for x in lst]

        if d["kind"] == "private":
            groups["private"].append(d)
        else:
            if d["name"].startswith("霊の湯2階"):
                groups["reino_hall"].append(d)
            else:
                groups["kami_hall"].append(d)
    con.close(); return groups

@app.get("/api/snapshot_immediate")
def api_snapshot_immediate():
    rooms = fetch_rooms_grouped()
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    reino = cur.execute("SELECT id, capacity FROM rooms WHERE name='霊の湯2階座敷'").fetchone()
    kami  = cur.execute("SELECT id, capacity FROM rooms WHERE name='神の湯2階座敷'").fetchone()
    reino_remain = (reino[1] - hall_seats_used(cur, reino[0])) if reino else 0
    kami_remain  = (kami[1]  - hall_seats_used(cur, kami[0]))  if kami  else 0
    con.close()
    return {"rooms": rooms, "summary": {"reino_remain": reino_remain, "kami_remain": kami_remain}}

# ===== 受付→即割当 API（待機/heading なし） =====
@app.post("/api/quick_assign")
def api_quick_assign(payload: dict):
    target = payload.get("targetArea", "private")
    headcount = int(payload.get("headcount", 1))
    room_id = payload.get("roomId")  # 個室のみ

    if target not in ("private","reino_hall","kami_hall"):
        raise HTTPException(400, "targetArea が不正です")
    if headcount <= 0:
        raise HTTPException(400, "人数は1以上にしてください")

    day_key = today_key()
    con = sqlite3.connect(DB_PATH, isolation_level=None); cur = con.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT COALESCE(MAX(seq),0)+1 FROM requests WHERE day_key=?", (day_key,))
        seq = int(cur.fetchone()[0])

        if target == "private":
            if not room_id: raise HTTPException(400, "個室には roomId が必要です")
            room_id = int(room_id)
            r = cur.execute("SELECT status, kind FROM rooms WHERE id=?", (room_id,)).fetchone()
            if not r: raise HTTPException(404, "部屋が見つかりません")
            r_status, r_kind = r
            if r_kind != 'private': raise HTTPException(409, "個室以外は選べません（系統を確認）")
            if r_status != 'available': raise HTTPException(409, "その個室は空室ではありません")

            cur.execute("""
                INSERT INTO requests(headcount,status,assigned_room_id,day_key,seq,target_area,allocated_seats,updated_at)
                VALUES(?, 'in_room', ?, ?, ?, 'private', NULL, CURRENT_TIMESTAMP)
            """,(headcount, room_id, day_key, seq))

            eta = (now_jst() + timedelta(minutes=105)).isoformat()
            cur.execute("UPDATE rooms SET status='occupied', eta_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (eta, room_id))

        else:
            hall_name = "霊の湯2階座敷" if target=="reino_hall" else "神の湯2階座敷"
            r = cur.execute("SELECT id, capacity, status FROM rooms WHERE name=?", (hall_name,)).fetchone()
            if not r: raise HTTPException(404, f"{hall_name} が見つかりません")
            rid, cap, rstatus = r
            if rstatus == 'disabled': raise HTTPException(409, f"{hall_name} は使用停止中です")

            need = seats_needed_for_group(headcount)
            used = hall_seats_used(cur, rid)
            if used + need > cap:
                raise HTTPException(409, f"{hall_name} の残り座席が不足（必要 {need} / 残り {max(0, cap-used)}）")

            cur.execute("""
                INSERT INTO requests(headcount,status,assigned_room_id,day_key,seq,target_area,allocated_seats,updated_at)
                VALUES(?, 'in_room', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,(headcount, rid, day_key, seq, target, need))

        con.commit(); return {"ok": True, "seq": seq}
    except HTTPException:
        con.rollback(); raise
    except Exception as e:
        con.rollback(); raise HTTPException(500, f"quick_assign error: {e}")
    finally:
        con.close()

# ===== 退室（個室→空室化、座敷→座席返却） =====
@app.post("/api/checkout")
def api_checkout(payload: dict):
    req_id = int(payload.get("requestId", 0))
    if not req_id: raise HTTPException(400, "bad params")
    con = sqlite3.connect(DB_PATH, isolation_level=None); cur = con.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute("""
            SELECT q.assigned_room_id, r.kind
              FROM requests q JOIN rooms r ON r.id=q.assigned_room_id
             WHERE q.id=? AND q.status='in_room'
        """,(req_id,)).fetchone()
        if not row: raise HTTPException(409, "入室中の依頼が見つかりません")
        room_id, kind = row

        cur.execute("UPDATE requests SET status='completed', allocated_seats=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (req_id,))
        if kind == 'private':
            cur.execute("UPDATE rooms SET status='available', eta_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (room_id,))

        con.commit(); return {"ok": True}
    except HTTPException:
        con.rollback(); raise
    except Exception as e:
        con.rollback(); raise HTTPException(500, f"checkout error: {e}")
    finally:
        con.close()

# ===== 個室 ETA 手動更新 =====
@app.post("/api/rooms/eta")
def api_rooms_eta(payload: dict):
    room_id = int(payload.get("roomId", 0))
    hhmm = payload.get("hhmm", "")
    if not room_id or not hhmm or len(hhmm)!=5:
        raise HTTPException(400, "bad params")
    try:
        h = int(hhmm[:2]); m = int(hhmm[3:5])
    except:
        raise HTTPException(400, "HH:MM 形式で指定してください")
    eta = now_jst().replace(hour=h, minute=m, second=0, microsecond=0).isoformat()

    con = sqlite3.connect(DB_PATH, isolation_level=None); cur = con.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT kind FROM rooms WHERE id=?", (room_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "部屋が見つかりません")
        if row[0] != 'private': raise HTTPException(409, "座敷の空き予定は手動設定できません")
        cur.execute("UPDATE rooms SET eta_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='occupied'", (eta, room_id))
        if cur.rowcount != 1: raise HTTPException(409, "使用中の個室のみ空き予定を変更できます")
        con.commit(); return {"ok": True}
    except HTTPException:
        con.rollback(); raise
    except Exception as e:
        con.rollback(); raise HTTPException(500, f"eta error: {e}")
    finally:
        con.close()
