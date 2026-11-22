"""
דאשבורד ווב — עמוד אחד, קריאה בלבד, מעל הנתונים שניקול כבר מנהלת.
נגיש ב-/dashboard?key=<DASHBOARD_TOKEN> — קישור סודי, בלי מסך התחברות.

הפילוסופיה הפוכה מ-Miles OS: שם הדאשבורד הוא המוצר; כאן הוא רק חלון
תצוגה. שום דבר לא נערך מכאן — הכל דרך השיחה עם ניקול.
"""

import os
import html
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import db, habits, nutrition, ibkr, memfiles

TZ = ZoneInfo("Asia/Jerusalem")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")


def token_ok(key: str) -> bool:
    return bool(DASHBOARD_TOKEN) and key == DASHBOARD_TOKEN


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _spark(values: list[float], w: int = 260, h: int = 48) -> str:
    """גרף SVG קטן לשווי התיק — בלי שום ספרייה."""
    vals = [v for v in values if v]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = w / (len(vals) - 1)
    pts = " ".join(f"{i*step:.1f},{h - (v-lo)/span*(h-6) - 3:.1f}"
                   for i, v in enumerate(vals))
    color = "#4CC38A" if vals[-1] >= vals[0] else "#E5484D"
    area = f"0,{h} {pts} {w},{h}"
    return (f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" preserveAspectRatio="none" class="spark">'
            f'<polygon fill="{color}18" points="{area}"/>'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{pts}"/></svg>')


async def render(chat_id: int, fetch_events=None, view: str = "today", key: str = "") -> str:
    now = datetime.now(TZ)

    # --- יומן גוגל: היום ומחר, או שבוע שלם במסך השבועי ---
    HE_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
    week_view = view == "week"
    events_html = ""
    if fetch_events:
        try:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            span = 7 if week_view else 2
            events = await fetch_events(start, start + timedelta(days=span))
            events = sorted(events, key=lambda e: e["start"])
            rows = []
            if week_view:
                by_day = {}
                for ev in events:
                    by_day.setdefault(ev["start"].date(), []).append(ev)
                for i in range(7):
                    d = now.date() + timedelta(days=i)
                    label = "היום" if i == 0 else "מחר" if i == 1 else HE_DAYS[d.weekday()]
                    rows.append(f'<li class="dayhead">{label} · {d.strftime("%d/%m")}</li>')
                    day_events = by_day.get(d, [])
                    if not day_events:
                        rows.append('<li class="dim">פנוי</li>')
                    for ev in day_events:
                        loc = f' <span class="dim">{_esc(ev.get("location"))}</span>' if ev.get("location") else ""
                        rows.append(f'<li><b>{ev["start"].strftime("%H:%M")}</b> {_esc(ev["summary"])}{loc}</li>')
            else:
                for ev in events[:12]:
                    day = "היום" if ev["start"].date() == now.date() else "מחר"
                    loc = f' <span class="dim">{_esc(ev.get("location"))}</span>' if ev.get("location") else ""
                    rows.append(f'<li><b>{day} {ev["start"].strftime("%H:%M")}</b> {_esc(ev["summary"])}{loc}</li>')
            events_html = "\n".join(rows) or '<li class="dim">היומן פנוי</li>'
        except Exception:
            events_html = '<li class="dim">שגיאה בטעינת היומן</li>'

    # --- משימות ---
    tasks = db.list_open_tasks(chat_id)
    task_rows = []
    for t in tasks[:15]:
        due = f' <span class="tag">⏳ {_esc(t["soft_due"])}</span>' if t.get("soft_due") else ""
        task_rows.append(f'<li>{_esc(t["title"])}{due}</li>')
    tasks_html = "\n".join(task_rows) or '<li class="dim">אין משימות פתוחות 🎉</li>'

    # --- הרגלים: שורת נקודות של 7 הימים האחרונים לכל הרגל ---
    habit_rows = []
    week = [(now.date() - timedelta(days=i)) for i in range(6, -1, -1)]
    for h in db.list_habits(chat_id):
        dates = set(db.habit_dates(h["id"]))
        streak = habits._streak(sorted(dates), now.date())
        dots = "".join(
            f'<i class="{"don" if d.isoformat() in dates else "off"}"></i>' for d in week)
        done_mark = "✅" if now.date().isoformat() in dates else "⬜"
        fire = f' <span class="fire">🔥{streak}</span>' if streak >= 2 else ""
        habit_rows.append(
            f'<li class="hab">{done_mark} {_esc(h["name"])}{fire}'
            f'<span class="dots">{dots}</span></li>')
    habits_html = "\n".join(habit_rows) or '<li class="dim">אין הרגלים עדיין — תגיד לניקול "תעקבי אחרי X"</li>'

    # --- תזונה ---
    kcal, protein = nutrition.totals(chat_id, now.strftime("%Y-%m-%d"))
    meals = db.meals_for_date(chat_id, now.strftime("%Y-%m-%d"))
    meal_rows = [f'<li><b>{m["meal_time"]}</b> {_esc(m["description"])}' +
                 (f' <span class="dim">{m["kcal"]} קק״ל</span>' if m.get("kcal") else "") + "</li>"
                 for m in meals]
    nutrition_html = "\n".join(meal_rows) or '<li class="dim">לא נרשמו ארוחות היום</li>'

    # --- תיק השקעות ---
    port = ibkr.summary()
    if port:
        hist = db.portfolio_history(limit=30)
        spark = _spark([r["nav"] for r in reversed(hist)])
        change = ""
        if port["change_pct"] is not None:
            cls = "up" if port["change"] >= 0 else "down"
            sign = "+" if port["change"] >= 0 else ""
            change = f'<span class="{cls}">{sign}{port["change_pct"]}% · {sign}${port["change"]:,.0f}</span>'
        def _chg_cell(p):
            c = p.get("chg")
            if c is None:
                return '<td class="dim">—</td>'
            cls = "up" if c >= 0 else "down"
            return f'<td class="{cls}">{"+" if c >= 0 else ""}{c}%</td>'
        pos_rows = "".join(
            f'<tr><td>{_esc(p["symbol"])}</td><td>{p["qty"]:g}</td>'
            f'<td>${p["value"]:,.0f}</td>{_chg_cell(p)}</tr>' for p in port["positions"][:8])
        portfolio_html = (
            f'<table><tr><th>נייר</th><th>כמות</th><th>שווי</th><th>שינוי</th></tr>{pos_rows}</table>'
            f'<div class="dim">מזומן: ${port["cash"]:,.0f} · נכון ל-{_esc(port["date"])}</div>')
    else:
        portfolio_html = '<div class="dim">אין נתונים עדיין — צריך להגדיר IBKR_FLEX_TOKEN</div>'

    # --- יומן אישי ---
    journal = memfiles.recent_journal(days=3)
    if journal:
        j_parts = []
        for line in journal[-1500:].splitlines():
            line = line.strip()
            if line.startswith("## "):
                j_parts.append(f'<div class="jday">{_esc(line[3:])}</div>')
            elif line.startswith("- "):
                body = line[2:]
                time_txt, sep, rest = body.partition(" — ")
                if sep and len(time_txt) == 5 and time_txt[2:3] == ":":
                    j_parts.append(f'<div class="jent"><b>{_esc(time_txt)}</b> {_esc(rest)}</div>')
                else:
                    j_parts.append(f'<div class="jent">{_esc(body)}</div>')
            elif line:
                j_parts.append(f'<div class="jent">{_esc(line)}</div>')
        journal_html = "\n".join(j_parts)
    else:
        journal_html = '<div class="dim">היומן ריק בינתיים — ייכתב מעצמו מסיכומי הערב</div>'

    # --- נתונים לכותרת, ל-Hero ולצ'יפים ---
    heb_date = ""
    try:
        from app import policy
        heb_date = policy.hebrew_date(now) or ""
    except Exception:
        pass
    weather_txt = ""
    try:
        from app import weather as _w
        weather_txt = (await _w.today_line() or "").replace("🌤 ", "").strip()
    except Exception:
        pass
    hour = now.hour
    greeting = ("בוקר טוב" if 5 <= hour < 12 else "צהריים טובים" if 12 <= hour < 17
                else "ערב טוב" if 17 <= hour < 22 else "לילה טוב")
    habit_rows_data = habits.status(chat_id)
    top_streak = max((r["streak"] for r in habit_rows_data), default=0)
    done_count = sum(1 for r in habit_rows_data if r["done_today"])
    kcal_goal = int(os.getenv("NUTRITION_KCAL_GOAL", "2000"))
    kcal_pct = min(100, round(kcal * 100 / kcal_goal)) if kcal else 0

    # גרף תזונה שבועי: קק"ל ליום, 7 ימים אחורה, עם קו היעד
    week_days = [(now.date() - timedelta(days=i)) for i in range(6, -1, -1)]
    week_kcal = [nutrition.totals(chat_id, d.strftime("%Y-%m-%d"))[0] for d in week_days]
    peak = max(week_kcal + [kcal_goal]) or 1
    BW, BH, GAP = 46, 78, 10
    chart_w = 7 * BW + 6 * GAP
    bars = []
    for i, (d, v) in enumerate(zip(week_days, week_kcal)):
        bh = round(v / peak * (BH - 14)) if v else 2
        x = i * (BW + GAP)
        color = "#FDE68A" if v <= kcal_goal else "#F87171"
        label = "היום" if d == now.date() else d.strftime("%d/%m")
        bars.append(
            f'<rect x="{x}" y="{BH - bh - 12}" width="{BW}" height="{bh}" rx="4" fill="{color}"'
            f'{" opacity=\'.55\'" if d != now.date() else ""}/>'
            f'<text x="{x + BW/2}" y="{BH}" text-anchor="middle" fill="#9AA0C3" font-size="9">{label}</text>')
    goal_y = round(BH - 12 - (kcal_goal / peak * (BH - 14)))
    nutrition_chart = (
        f'<svg width="100%" viewBox="0 0 {chart_w} {BH + 2}" class="nchart">'
        f'<line x1="0" y1="{goal_y}" x2="{chart_w}" y2="{goal_y}" stroke="#9AA0C3" '
        f'stroke-width="1" stroke-dasharray="4 4" opacity=".6"/>{"".join(bars)}</svg>')

    bot_link = os.getenv("NIKOL_BOT_LINK", "")
    talk_btn = '<button class="talk" onclick="npOpen()">💬 ניקול</button>'

    # Hero: התיק אם יש נתונים, אחרת כרטיס היום
    if port:
        pill = ""
        if port["change_pct"] is not None:
            up = port["change"] >= 0
            cls = "pill-up" if up else "pill-down"
            sign = "+" if up else ""
            arrow = "▲" if up else "▼"
            pill = (f'<span class="pill {cls}">{arrow} {sign}{port["change_pct"]}% · '
                    f'{sign}${port["change"]:,.0f} מאתמול</span>')
        hist = db.portfolio_history(limit=30)
        spark = _spark([r["nav"] for r in reversed(hist)])
        hero_html = (f'<div class="k">שווי התיק · IBKR · {_esc(port["date"])}</div>'
                     f'<div class="v">${port["nav"]:,.0f}</div>{pill}{spark}')
    else:
        n_ev = events_html.count("<li>")
        hero_html = (f'<div class="k">היום שלך</div>'
                     f'<div class="v">{n_ev} אירועים</div>'
                     f'<span class="pill pill-dim">התיק יופיע כאן אחרי הגדרת IBKR</span>')

    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>ניקול</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {{
    --text:#F2F4FF; --dim:#9AA0C3; --glass:rgba(255,255,255,.055);
    --edge:rgba(255,255,255,.11); --hair:rgba(255,255,255,.07);
    --sky:#7DD3FC; --amber:#FDE68A; --pink:#FCA5F1; --up:#4ADE80; --down:#F87171;
    color-scheme: dark;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; color:var(--text); font-family:"Rubik",system-ui,sans-serif;
    font-size:15px; line-height:1.55; padding:clamp(14px,3vw,32px);
    min-height:100vh; background:#151329;
    background-image:
      radial-gradient(circle at 85% -10%, #3B2A6E 0%, rgba(21,19,41,0) 55%),
      radial-gradient(circle at 0% 100%, #0E3A5C 0%, rgba(21,19,41,0) 50%);
    background-attachment:fixed; }}
  h1 {{ font-size:clamp(1.35rem,4vw,1.7rem); margin:0; font-weight:800; }}
  .sub {{ color:var(--dim); font-size:.82rem; margin:2px 0 16px; }}

  .hero {{ background:var(--glass); border:1px solid var(--edge); border-radius:22px;
    padding:18px 20px 14px; margin-bottom:12px;
    backdrop-filter:blur(14px); -webkit-backdrop-filter:blur(14px); }}
  .hero .k {{ color:var(--dim); font-size:.74rem; letter-spacing:.1em; }}
  .hero .v {{ font-size:clamp(1.9rem,7vw,2.4rem); font-weight:800; margin:2px 0 6px; }}
  .pill {{ display:inline-block; border-radius:99px; padding:2px 12px;
    font-size:.8rem; font-weight:600; }}
  .pill-up {{ background:rgba(74,222,128,.14); color:var(--up); }}
  .pill-down {{ background:rgba(248,113,113,.14); color:var(--down); }}
  .pill-dim {{ background:rgba(255,255,255,.08); color:var(--dim); }}
  .spark {{ display:block; margin-top:12px; opacity:.9; }}

  .chips {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
  .chip {{ background:var(--glass); border:1px solid var(--edge); border-radius:15px;
    padding:8px 14px; font-size:.75rem; color:var(--dim);
    backdrop-filter:blur(14px); -webkit-backdrop-filter:blur(14px); }}
  .chip b {{ display:block; font-size:1.05rem; font-weight:800; color:var(--text); }}
  .chip.c1 b {{ color:var(--pink); }} .chip.c2 b {{ color:var(--sky); }}
  .chip.c3 b {{ color:var(--amber); }} .chip.c4 b {{ color:var(--up); }}

  main {{ max-width:680px; margin:0 auto; }}
  .grid {{ display:grid; gap:12px; grid-template-columns:1fr; }}
  .card {{ background:var(--glass); border:1px solid var(--edge); border-radius:18px;
    padding:14px 17px; backdrop-filter:blur(14px); -webkit-backdrop-filter:blur(14px); }}
  .card h2 {{ font-size:.86rem; margin:0 0 10px; font-weight:700; }}
  ul {{ margin:0; padding:0; list-style:none; }}
  li {{ padding:6px 0; font-size:.9rem; border-bottom:1px solid var(--hair); }}
  li:last-child {{ border-bottom:none; }}
  li b {{ background:rgba(125,211,252,.14); color:var(--sky); border-radius:8px;
    padding:1px 8px; font-size:.74rem; font-weight:700; margin-inline-end:6px; }}
  .dim {{ color:var(--dim); }} .tag {{ color:var(--amber); font-size:.78rem; }}
  .fire {{ color:var(--amber); font-weight:700; }}

  .prog {{ height:8px; background:rgba(255,255,255,.08); border-radius:99px;
    margin:2px 0 10px; }}
  .prog i {{ display:block; height:8px; border-radius:99px; background:var(--amber);
    width:{kcal_pct}%; }}

  .nav {{ font-size:1.5rem; font-weight:800; display:flex; align-items:center;
    gap:10px; flex-wrap:wrap; margin-bottom:4px; }}
  .up {{ color:var(--up); font-size:.82rem; font-weight:600; }}
  .down {{ color:var(--down); font-size:.82rem; font-weight:600; }}
  table {{ width:100%; border-collapse:collapse; font-size:.84rem; margin:6px 0 4px; }}
  th {{ text-align:right; color:var(--dim); font-weight:400; font-size:.72rem;
    padding:4px 0; border-bottom:1px solid var(--edge); }}
  td {{ padding:6px 0; border-bottom:1px solid var(--hair); }}
  tr:last-child td {{ border-bottom:none; }}

  .jday {{ color:var(--pink); font-weight:700; font-size:.82rem; margin:10px 0 2px; }}
  .jday:first-child {{ margin-top:0; }}
  .jent {{ font-size:.88rem; padding:4px 0; border-bottom:1px solid var(--hair); }}
  .jent:last-child {{ border-bottom:none; }}
  .jent b {{ background:none; color:var(--dim); padding:0; font-size:.74rem;
    font-weight:600; margin-inline-end:8px; }}

  .tabs {{ display:flex; gap:8px; margin-bottom:12px; }}
  .tabs a {{ color:var(--dim); text-decoration:none; font-size:.86rem; font-weight:600;
    padding:6px 16px; border-radius:99px; border:1px solid var(--edge);
    background:var(--glass); }}
  .tabs a.on {{ color:#151329; background:var(--sky); border-color:var(--sky); }}
  .dayhead {{ color:var(--pink); font-weight:700; font-size:.8rem;
    border-bottom:none !important; padding-top:12px !important; }}
  .nchart {{ display:block; margin:4px 0 10px; }}
  .hab {{ display:flex; align-items:center; gap:6px; }}
  .dots {{ margin-inline-start:auto; display:flex; gap:4px; }}
  .dots i {{ width:9px; height:9px; border-radius:50%; display:block; }}
  .dots .don {{ background:var(--up); }}
  .dots .off {{ background:rgba(255,255,255,.12); }}
  .talk {{ position:fixed; bottom:18px; inset-inline-start:18px; background:var(--sky);
    color:#151329; border:none; cursor:pointer; border-radius:99px; padding:12px 20px;
    font-weight:700; font-size:.92rem; font-family:inherit; z-index:40;
    box-shadow:0 6px 20px rgba(125,211,252,.35); }}
  #np {{ position:fixed; top:0; inset-inline-start:0; height:100dvh;
    width:min(390px,94vw); z-index:50; display:flex; flex-direction:column;
    background:rgba(21,19,41,.93); border-inline-end:1px solid var(--edge);
    backdrop-filter:blur(22px); -webkit-backdrop-filter:blur(22px);
    transform:translateX(110%); transition:transform .28s ease; }}
  #np.open {{ transform:translateX(0); }}
  #np header {{ display:flex; align-items:center; gap:10px; padding:14px 16px;
    border-bottom:1px solid var(--edge); font-weight:700; }}
  #np header .x {{ margin-inline-start:auto; background:none; border:none;
    color:var(--dim); font-size:1.25rem; cursor:pointer; }}
  #msgs {{ flex:1; overflow-y:auto; padding:14px 16px; display:flex;
    flex-direction:column; gap:8px; }}
  .m {{ max-width:85%; padding:8px 13px; border-radius:15px; font-size:.9rem;
    white-space:pre-wrap; word-break:break-word; }}
  .m.me {{ align-self:flex-start; background:var(--sky); color:#151329;
    border-end-start-radius:4px; }}
  .m.nk {{ align-self:flex-end; background:var(--glass); border:1px solid var(--edge);
    border-end-end-radius:4px; }}
  .m.wait i {{ animation:blink 1.2s infinite; }}
  @keyframes blink {{ 50% {{ opacity:.25; }} }}
  #npform {{ display:flex; gap:8px; padding:12px 14px; border-top:1px solid var(--edge); }}
  #npin {{ flex:1; background:var(--glass); border:1px solid var(--edge); color:var(--text);
    border-radius:12px; padding:10px 13px; font-family:inherit; font-size:.92rem; }}
  #npin:focus {{ outline:2px solid var(--sky); outline-offset:1px; }}
  #npsend {{ background:var(--sky); color:#151329; border:none; border-radius:12px;
    padding:0 18px; font-weight:700; font-family:inherit; cursor:pointer; }}
  footer {{ color:var(--dim); font-size:.72rem; margin-top:18px; text-align:center; }}
  @media (prefers-reduced-motion: no-preference) {{
    .hero, .chip, .card {{ animation:rise .45s ease both; }}
    @keyframes rise {{ from {{ opacity:0; transform:translateY(8px); }} }}
  }}
</style></head><body>

<main>
<h1>{greeting}, רון ☀️</h1>
<div class="sub">{now.strftime("%d/%m/%Y")}{f" · {heb_date}" if heb_date else ""}{f" · {weather_txt}" if weather_txt else ""} · עודכן {now.strftime("%H:%M")}</div>

<nav class="tabs">
  <a href="?key={key}&view=today" class="{'on' if not week_view else ''}">היום</a>
  <a href="?key={key}&view=week" class="{'on' if week_view else ''}">השבוע</a>
</nav>

<div class="hero">{hero_html}</div>

<div class="chips">
  <div class="chip c1"><b>🔥 {top_streak}</b>הרצף הארוך</div>
  <div class="chip c2"><b>{done_count}/{len(habit_rows_data)}</b>הרגלים היום</div>
  <div class="chip c3"><b>{kcal:,}</b>קק"ל היום</div>
  <div class="chip c4"><b>{protein}g</b>חלבון</div>
</div>

<div class="grid">
  <div class="card"><h2>📅 {"השבוע" if week_view else "היום ומחר"}</h2><ul>{events_html}</ul></div>
  <div class="card"><h2>📋 משימות</h2><ul>{tasks_html}</ul></div>
  <div class="card"><h2>🔁 הרגלים</h2><ul>{habits_html}</ul></div>
  <div class="card"><h2>🍽 תזונה · {kcal:,}/{kcal_goal:,} קק"ל</h2>
    <div class="prog"><i></i></div>{nutrition_chart}<ul>{nutrition_html}</ul></div>
  <div class="card"><h2>📈 פוזיציות</h2>{portfolio_html}</div>
  <div class="card"><h2>📓 היומן האישי</h2><div class="journal">{journal_html}</div></div>
</div>

<footer>ניקול · מתעדכן כל 5 דקות · הכל מנוהל בשיחה</footer>
{talk_btn}
</main>
<script>const NP_KEY = "{key}";</script>
{CHAT_PANEL}
</body></html>"""


CHAT_PANEL = """
<div id="np" dir="rtl">
  <header>💬 ניקול <span class="dim" style="font-weight:400;font-size:.78rem">מחוברת לשרת</span>
    <button class="x" onclick="npClose()" aria-label="סגירה">✕</button></header>
  <div id="msgs"><div class="m nk">היי רון 👋 אפשר לשאול אותי כאן כל דבר — היומן, המשימות, התיק. פעולות שדורשות אישור אשלח לך לטלגרם.</div></div>
  <div id="npform">
    <input id="npin" placeholder="כתוב לניקול..." autocomplete="off">
    <button id="npsend" onclick="npSend()">שלח</button>
  </div>
</div>
<script>
const np = document.getElementById('np'), msgs = document.getElementById('msgs'),
      npin = document.getElementById('npin');
function npOpen(){ np.classList.add('open'); npin.focus(); }
function npClose(){ np.classList.remove('open'); }
npin.addEventListener('keydown', e => { if (e.key === 'Enter') npSend(); });
function add(cls, text){
  const d = document.createElement('div'); d.className = 'm ' + cls;
  d.textContent = text; msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight; return d;
}
async function npSend(){
  const text = npin.value.trim(); if (!text) return;
  npin.value = ''; add('me', text);
  const w = add('nk wait', ''); w.innerHTML = '<i>●</i> <i>●</i> <i>●</i>';
  try {
    const r = await fetch('dashboard/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: NP_KEY, text})
    });
    const j = await r.json();
    w.classList.remove('wait');
    w.textContent = j.reply || 'שלחתי לך הצעת אישור לטלגרם 📲';
  } catch (e) {
    w.classList.remove('wait');
    w.textContent = 'החיבור נכשל — נסה שוב.';
  }
  msgs.scrollTop = msgs.scrollHeight;
}
</script>
"""
