# -*- coding: utf-8 -*-
"""
БУРМАШ · CRM Дэшборд (v5.1)
— Переключатель агрегации оси времени (Авто/Дни/Недели/Месяцы)
— Фильтр по отделам: «Только отдел продаж» + мультивыбор отделов
— Остальной функционал — как в v5.0: период НИТ/Год/Квартал/Месяц/Неделя, динамика к прошлому периоду,
  корректная выручка/выиграно/проиграно по трём воронкам, «Провал» по причинам/группам, менеджеры,
  годовой план/факт/прогноз, AI-рекомендации с флагами «обходов».
"""

import os, time, math, calendar
from datetime import datetime, timedelta, date
import numpy as np
import pandas as pd
import streamlit as st
import requests

try:
    import plotly.express as px
except Exception:
    px = None

# ============ UI ============
st.set_page_config(page_title="БУРМАШ · CRM", page_icon="🟧", layout="wide")
THEME_CSS = """
<style>
:root{ --brand:#ff7a00; --black:#0a0a0a; --border:#e9edf2; --muted:#6b7280; --good:#10b981; --bad:#ef4444; --warn:#f59e0b; }
html,body,[data-testid="stAppViewContainer"]{ background:#ffffff; color:var(--black); }
.block-container{ padding-top:.6rem; padding-bottom:1.2rem; }
.card{ background:#fff; border:1px solid var(--border); border-radius:14px; padding:16px 16px 10px; box-shadow:0 2px 12px rgba(0,0,0,.04); }
.title{ font-weight:700; font-size:18px; margin-bottom:6px; }
.subtle{ color:var(--muted); font-size:12px; }
.badge{ display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:999px; border:1px solid var(--border); background:#fff; font-size:12px; margin-right:6px; margin-bottom:6px;}
.badge.good{ color:var(--good); border-color:rgba(16,185,129,.3); }
.badge.bad{ color:var(--bad); border-color:rgba(239,68,68,.3); }
.badge.warn{ color:var(--warn); border-color:rgba(245,158,11,.3); }
.kpi{ font-weight:800; font-size:28px; line-height:1; }
.kpi-caption{ color:var(--muted); font-size:12px; margin-top:-6px }
.pill{ display:inline-block; padding:8px 12px; border-radius:12px; background:rgba(255,122,0,.08); color:var(--brand); font-weight:800; border:1px solid rgba(255,122,0,.25); }
hr{ border:0; border-top:1px solid var(--border); margin:10px 0 8px }
div[data-testid="stMetricValue"]{ font-size:22px !important; }
.score{ width:64px;height:64px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;background:#fff;border:2px solid var(--border);font-weight:800;font-size:22px;margin-right:10px }
.grid2{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.grid3{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
.headerbar{ display:flex; align-items:center; gap:16px; margin-bottom:8px; }
.small{ font-size:12px; color:var(--muted); }
</style>
"""
st.markdown(THEME_CSS, unsafe_allow_html=True)

# ============ AUTH ============
AUTH_KEY = "burmash_auth_ok"
def require_auth():
    if AUTH_KEY not in st.session_state:
        st.session_state[AUTH_KEY] = False
    if st.session_state[AUTH_KEY]:
        return
    st.markdown("### 🔐 Вход — БУРМАШ")
    with st.form("login_form", clear_on_submit=False):
        login = st.text_input("Логин", value="", key="auth_user")
        password = st.text_input("Пароль", value="", type="password", key="auth_pass")
        ok = st.form_submit_button("Войти")
    if ok:
        st.session_state[AUTH_KEY] = (login == "admin" and password == "admin123")
        if not st.session_state[AUTH_KEY]:
            st.error("Неверный логин или пароль")
        st.rerun()
    st.stop()
require_auth()
with st.sidebar:
    if st.button("Выйти", key="logout_btn"):
        st.session_state[AUTH_KEY] = False
        st.rerun()

# ============ Secrets ============
def get_secret(name, default=None):
    if name in st.secrets: return st.secrets[name]
    return os.getenv(name, default)
BITRIX24_WEBHOOK   = (get_secret("BITRIX24_WEBHOOK", "") or "").strip()
PERPLEXITY_API_KEY = (get_secret("PERPLEXITY_API_KEY", "") or "").strip()

# ============ Bitrix helpers ============
def _bx_get(method, params=None, pause=0.4):
    url = BITRIX24_WEBHOOK.rstrip("/") + f"/{method}.json"
    out, start = [], 0
    params = dict(params or {})
    while True:
        params["start"] = start
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        res = data.get("result")
        batch = (res.get("items", []) if isinstance(res, dict) and "items" in res else res) or []
        if not batch: break
        out.extend(batch)
        if len(batch) < 50: break
        start += 50
        time.sleep(pause)
    return out

@st.cache_data(ttl=300)
def bx_get_deals(date_from=None, date_to=None, limit=3000):
    params = {"select[]":[
        "ID","TITLE","STAGE_ID","OPPORTUNITY","ASSIGNED_BY_ID","COMPANY_ID","CONTACT_ID",
        "PROBABILITY","DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME","CATEGORY_ID",
        "BEGINDATE","CLOSEDATE","STAGE_SEMANTIC_ID"
    ]}
    if date_from: params["filter[>=DATE_CREATE]"] = date_from
    if date_to:   params["filter[<=DATE_CREATE]"] = date_to
    deals = _bx_get("crm.deal.list", params)
    return deals[:limit]

@st.cache_data(ttl=300)
def bx_get_departments():
    try:
        return _bx_get("department.get", {})
    except:
        return []

@st.cache_data(ttl=300)
def bx_get_users_full():
    users = _bx_get("user.get", {})
    out = {}
    for u in users:
        depts = u.get("UF_DEPARTMENT") or []
        if isinstance(depts, str): depts = [int(x) for x in depts.split(",") if x]
        out[int(u["ID"])] = {
            "name": ((u.get("NAME","")+" "+u.get("LAST_NAME","")).strip() or u.get("LOGIN","")).strip(),
            "depts": list(map(int, depts)) if depts else [],
            "active": (u.get("ACTIVE","Y")=="Y")
        }
    return out

@st.cache_data(ttl=300)
def bx_get_activities(deal_ids, include_completed=True):
    out = {}
    if not deal_ids: return out
    states = ["N","Y"] if include_completed else ["N"]
    for state in states:
        for chunk in np.array_split(list(map(int, deal_ids)), max(1, len(deal_ids)//40 + 1)):
            params = {
                "filter[OWNER_TYPE_ID]":2, "filter[OWNER_ID]":",".join(map(str,chunk)),
                "filter[COMPLETED]": state
            }
            acts = _bx_get("crm.activity.list", params)
            for a in acts:
                out.setdefault(int(a["OWNER_ID"]), []).append(a)
    return out

@st.cache_data(ttl=600)
def bx_get_stage_map(stage_ids):
    sort_map, name_map = {}, {}
    if not BITRIX24_WEBHOOK or not stage_ids: return sort_map, name_map
    cats = set()
    for sid in stage_ids:
        if isinstance(sid, str) and sid.startswith("C"):
            try: cats.add(int(sid.split(":")[0][1:]))
            except: pass
    try:
        base = _bx_get("crm.status.list", {"filter[ENTITY_ID]":"DEAL_STAGE"})
        for s in base:
            sort_map[s.get("STATUS_ID")] = int(s.get("SORT", 5000))
            name_map[s.get("STATUS_ID")] = s.get("NAME") or s.get("STATUS_ID")
    except: pass
    for cid in cats:
        try:
            resp = _bx_get("crm.status.list", {"filter[ENTITY_ID]": f"DEAL_STAGE_{cid}"})
            for s in resp:
                sort_map[s.get("STATUS_ID")] = int(s.get("SORT", 5000))
                name_map[s.get("STATUS_ID")] = s.get("NAME") or s.get("STATUS_ID")
        except: continue
    return sort_map, name_map

@st.cache_data(ttl=600)
def bx_get_categories():
    try:
        cats = _bx_get("crm.category.list", {})
        return {int(c["ID"]): c.get("NAME","Воронка") for c in cats}
    except:
        return {}

# ============ Воронки и причины ============
CAT_MAIN   = "основная воронка продаж"
CAT_PHYS   = "физ.лица"
CAT_LOW    = "не приоритетные сделки"
SUCCESS_NAME_BY_CAT = {
    CAT_MAIN: "Успешно реализовано",
    CAT_PHYS: "Сделка успешна",
    CAT_LOW:  "Сделка успешна",
}
FAIL_GROUP1 = {
    "Недозвон","Не абонент","СПАМ","Нецелевой","Дорого","Организация не действует","Был конфликт",
    "Не одобрили отсрочку платежа","Не устроили сроки","Сделка отменена клиентом","Удалено из неразобр. Авито"
}
FAIL_GROUP2 = {
    "Выбрали конкурентов","Дорого","Был конфликт","Не одобрили отсрочку платежа","Не устроили сроки","Сделка отменена клиентом"
}

# ============ Даты/периоды ============
def to_dt(x):
    try:
        ts = pd.to_datetime(x, utc=True, errors="coerce")
        if pd.isna(ts): return pd.NaT
        return ts.tz_convert(None)
    except:
        return pd.NaT

def period_range(mode, start_date=None, year=None, quarter=None, month=None, iso_week=None):
    today = date.today()
    if mode == "НИТ":
        start = start_date or (today - timedelta(days=30)); end = today
    elif mode == "Год":
        y = int(year or today.year); start = date(y,1,1); end = date(y,12,31)
    elif mode == "Квартал":
        y = int(year or today.year); q = int(quarter or ((today.month-1)//3 + 1))
        m1 = 3*(q-1)+1; m2 = m1+2; start = date(y,m1,1); end = date(y,m2, calendar.monthrange(y,m2)[1])
    elif mode == "Месяц":
        y = int(year or today.year); m = int(month or today.month)
        start = date(y,m,1); end = date(y,m, calendar.monthrange(y,m)[1])
    elif mode == "Неделя":
        y = int(year or today.isocalendar().year); w = int(iso_week or today.isocalendar().week)
        start = pd.to_datetime(f"{y}-W{w}-1").date(); end = start + timedelta(days=6)
    else:
        start = today - timedelta(days=30); end = today
    return start, end

def previous_period(start, end):
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length-1)
    return prev_start, prev_end

def period_freq(mode):
    if mode == "НИТ": return "D"
    if mode == "Год": return "M"
    if mode == "Квартал": return "W-MON"
    if mode == "Месяц": return "D"
    if mode == "Неделя": return "D"
    return "D"

def freq_from_label(label):
    if label.startswith("Авто"): return None
    return {"Дни":"D","Недели":"W-MON","Месяцы":"M"}[label]

def ts_with_prev(df, date_col, value_col, start, end, mode, agg="sum", freq_override=None):
    if df.empty:
        return pd.DataFrame(columns=["period","value","prev_value"])
    m = (df[date_col].dt.date.between(start, end))
    cur = df.loc[m].copy()
    freq = freq_override or period_freq(mode)
    cur = cur.set_index(date_col).resample(freq)[value_col].agg(agg).rename("value")
    pstart, pend = previous_period(start, end)
    m2 = df[date_col].dt.date.between(pstart, pend)
    prev = df.loc[m2].copy().set_index(date_col).resample(freq)[value_col].agg(agg).rename("prev_value")
    out = pd.concat([cur, prev], axis=1).fillna(0).reset_index().rename(columns={date_col:"period"})
    return out

# ============ Скоринг/метки ============
def days_between(later, earlier):
    a, b = to_dt(later), to_dt(earlier)
    if pd.isna(a) or pd.isna(b): return None
    return max(0, int((a - b) / pd.Timedelta(days=1)))

def compute_health_scores(df, open_tasks_map, stuck_days=5):
    now = to_dt(pd.Timestamp.utcnow())
    rows = []
    for _, r in df.iterrows():
        create_dt = to_dt(r.get("DATE_CREATE"))
        last = to_dt(r.get("LAST_ACTIVITY_TIME")) or to_dt(r.get("DATE_MODIFY")) or create_dt
        begin_dt = to_dt(r.get("BEGINDATE")) or create_dt
        d_work  = days_between(now, create_dt) or 0
        d_noact = days_between(now, last) or 0
        d_stage = days_between(now, begin_dt) or 0
        has_task = len(open_tasks_map.get(int(r["ID"]), [])) > 0
        flags = {
            "no_company": int(r.get("COMPANY_ID") or 0) == 0,
            "no_contact": int(r.get("CONTACT_ID") or 0) == 0,
            "no_tasks": not has_task,
            "stuck": d_noact >= stuck_days,
            "lost": str(r.get("STAGE_ID","")).upper().find("LOSE") >= 0
        }
        score = 100
        if flags["no_company"]: score -= 10
        if flags["no_contact"]: score -= 10
        if flags["no_tasks"]:   score -= 25
        if flags["stuck"]:      score -= 25
        if flags["lost"]:       score = min(score, 15)
        opp  = float(r.get("OPPORTUNITY") or 0.0)
        prob = float(r.get("PROBABILITY")  or 0.0)
        potential = min(100, int((opp > 0) * (30 + min(70, math.log10(max(1, opp))/5 * 70)) * (0.4 + prob/100*0.6)))
        rows.append({
            "ID": int(r["ID"]),
            "TITLE": r.get("TITLE",""),
            "ASSIGNED_BY_ID": int(r.get("ASSIGNED_BY_ID") or 0),
            "STAGE_ID": r.get("STAGE_ID",""),
            "CATEGORY_ID": r.get("CATEGORY_ID"),
            "OPPORTUNITY": opp,
            "PROBABILITY": prob,
            "DATE_CREATE": create_dt,
            "DATE_MODIFY": to_dt(r.get("DATE_MODIFY")),
            "LAST_ACTIVITY_TIME": last,
            "BEGINDATE": begin_dt,
            "CLOSEDATE": to_dt(r.get("CLOSEDATE")),
            "SEMANTIC": (r.get("STAGE_SEMANTIC_ID") or "").upper(),
            "days_in_work": d_work,
            "days_no_activity": d_noact,
            "days_on_stage": d_stage,
            "health": max(0, min(100, int(score))),
            "potential": max(0, min(100, int(potential))),
            "flag_no_company": flags["no_company"],
            "flag_no_contact": flags["no_contact"],
            "flag_no_tasks": flags["no_tasks"],
            "flag_stuck": flags["stuck"],
            "flag_lost": flags["lost"],
        })
    return pd.DataFrame(rows)

def normalize_name(x): return str(x or "").strip().casefold()
def is_success(row, category_name, stage_name):
    cat = normalize_name(category_name)
    target = SUCCESS_NAME_BY_CAT.get(cat)
    return bool(target and (str(stage_name or "") == target))
def is_failure_reason(stage_name):
    name = str(stage_name or "")
    return (name in FAIL_GROUP1) or (name in FAIL_GROUP2)
def failure_group(stage_name):
    name = str(stage_name or "")
    if name in FAIL_GROUP1: return "Группа 1 (ранние этапы)"
    if name in FAIL_GROUP2: return "Группа 2 (поздние этапы)"
    return "Прочее"

def cheat_flags_for_deal(acts):
    if not acts: return {"reschedules":0, "micro_tasks":0}
    df = pd.DataFrame(acts)
    for col in ["CREATED","LAST_UPDATED","DEADLINE","START_TIME","END_TIME"]:
        if col in df: df[col] = pd.to_datetime(df[col], errors="coerce")
    reschedules = 0
    if "SUBJECT" in df.columns and "DEADLINE" in df.columns:
        for _, g in df.groupby("SUBJECT"):
            uniq = g["DEADLINE"].dropna().dt.floor("D").nunique()
            if uniq > 1: reschedules += (uniq - 1)
    micro = 0
    if "START_TIME" in df.columns and "END_TIME" in df.columns:
        dur = (df["END_TIME"] - df["START_TIME"]).dt.total_seconds() / 60.0
        micro += int((dur.dropna() <= 15).sum())
    return {"reschedules":int(reschedules), "micro_tasks":int(micro)}

# ============ Сайдбар: период/агрегация ============
st.sidebar.title("Фильтры периода")
mode = st.sidebar.selectbox("Режим периода", ["НИТ","Год","Квартал","Месяц","Неделя"], index=0)
if mode == "НИТ":
    start_input = st.sidebar.date_input("НИТ — с какой даты", datetime.now().date()-timedelta(days=30))
    year=quarter=month=iso_week=None
elif mode == "Год":
    year = st.sidebar.number_input("Год", min_value=2020, max_value=2100, value=datetime.now().year, step=1)
    start_input=month=quarter=iso_week=None
elif mode == "Квартал":
    year = st.sidebar.number_input("Год", min_value=2020, max_value=2100, value=datetime.now().year, step=1)
    quarter = st.sidebar.selectbox("Квартал", [1,2,3,4], index=(datetime.now().month-1)//3)
    start_input=month=iso_week=None
elif mode == "Месяц":
    year = st.sidebar.number_input("Год", min_value=2020, max_value=2100, value=datetime.now().year, step=1)
    month = st.sidebar.selectbox("Месяц", list(range(1,13)), index=datetime.now().month-1)
    start_input=quarter=iso_week=None
else: # Неделя
    year = st.sidebar.number_input("Год", min_value=2020, max_value=2100, value=datetime.now().isocalendar().year, step=1)
    iso_week = st.sidebar.number_input("ISO-неделя", min_value=1, max_value=53, value=datetime.now().isocalendar().week, step=1)
    start_input=quarter=month=None

st.sidebar.title("Агрегация графиков")
agg_label = st.sidebar.selectbox("Ось времени (агрегация)", ["Авто (от режима)","Дни","Недели","Месяцы"], index=0)
agg_freq = freq_from_label(agg_label)

stuck_days = st.sidebar.slider("Нет активности ≥ (дней)", 2, 21, 5)
limit      = st.sidebar.slider("Лимит сделок (API)", 50, 3000, 1000, step=50)

# ============ Загрузка ============
with st.spinner("Загружаю данные…"):
    if not BITRIX24_WEBHOOK:
        st.error("Не указан BITRIX24_WEBHOOK в Secrets."); st.stop()
    start, end = period_range(mode, start_date=start_input, year=year, quarter=quarter, month=month, iso_week=iso_week)
    deals_raw = bx_get_deals(str(start), str(end), limit=limit)
    if not deals_raw:
        st.error("Сделок не найдено за период."); st.stop()
    df_raw = pd.DataFrame(deals_raw)
    df_raw["OPPORTUNITY"] = pd.to_numeric(df_raw.get("OPPORTUNITY"), errors="coerce").fillna(0.0)
    users_full = bx_get_users_full()
    users_map = {uid: users_full[uid]["name"] for uid in users_full}
    categories_map = bx_get_categories()
    activities_map = bx_get_activities(df_raw["ID"].tolist(), include_completed=True)

# Основная таблица
df = compute_health_scores(df_raw, {k:v for k,v in activities_map.items() if v}, stuck_days=stuck_days)
stage_ids = df["STAGE_ID"].dropna().unique().tolist()
sort_map, name_map = bx_get_stage_map(stage_ids)

FALLBACK_ORDER = ["NEW","NEW_LEAD","PREPARATION","PREPAYMENT_INVOICE","EXECUTING","FINAL_INVOICE","WON","LOSE"]
def fallback_sort(sid):
    sid = str(sid or ""); sid_short = sid.split(":")[1] if ":" in sid else sid
    return (FALLBACK_ORDER.index(sid_short)*100 if sid_short in FALLBACK_ORDER else 10000 + hash(sid_short)%1000)

df["stage_sort"] = df["STAGE_ID"].map(lambda s: sort_map.get(str(s), fallback_sort(s)))
df["stage_name"] = df["STAGE_ID"].map(lambda s: name_map.get(str(s), str(s)))
df["manager"]    = df["ASSIGNED_BY_ID"].map(users_map).fillna("Неизвестно")
df["category"]   = df["CATEGORY_ID"].map(lambda x: categories_map.get(int(x or 0), "Воронка"))
df["cat_norm"]   = df["category"].map(normalize_name)
df["is_success"] = df.apply(lambda r: is_success(r, r["cat_norm"], r["stage_name"]), axis=1)
df["is_fail"]    = df["stage_name"].map(is_failure_reason)
df["fail_group"] = df["stage_name"].map(failure_group)
df["reschedules"] = df["ID"].map(lambda i: cheat_flags_for_deal(activities_map.get(int(i)))["reschedules"])
df["micro_tasks"] = df["ID"].map(lambda i: cheat_flags_for_deal(activities_map.get(int(i)))["micro_tasks"])
df["cheat_flag"]  = (df["reschedules"]>=3) | (df["micro_tasks"]>=5)

# ============ Фильтр по отделам ============
st.sidebar.title("Отделы / сотрудники")
departments = bx_get_departments()
sales_depts = [d for d in departments if "продаж" in (d.get("NAME","").lower())]
sales_dept_ids = {int(d["ID"]) for d in sales_depts}
default_sales_only = bool(sales_dept_ids)

sales_only = st.sidebar.checkbox("Только отдел продаж", value=default_sales_only)
dept_options = [(int(d["ID"]), d["NAME"]) for d in departments]
selected_depts = st.sidebar.multiselect(
    "Выбор отделов",
    options=dept_options,
    default=[(int(d["ID"]), d["NAME"]) for d in sales_depts] if sales_only else [],
    format_func=lambda t: t[1] if isinstance(t, tuple) else str(t)
)
selected_dept_ids = {t[0] for t in selected_depts} if selected_depts else (sales_dept_ids if sales_only else set())

if selected_dept_ids:
    keep_users = [uid for uid, info in users_full.items() if set(info["depts"]) & selected_dept_ids]
    if keep_users:
        df = df[df["ASSIGNED_BY_ID"].isin(keep_users)]

# ============ Шапка ============
st.markdown("<div class='headerbar'><div class='pill'>БУРМАШ · Контроль отдела продаж</div></div>", unsafe_allow_html=True)
st.caption(f"Период: {start} → {end}. Динамика — к предыдущему периоду той же длины. Агрегация графиков: {agg_label}.")

# ============ Вкладки ============
tab_over, tab_prob, tab_mgr, tab_grad, tab_time, tab_ai, tab_plan = st.tabs([
    "📊 Обзор", "⚠️ Проблемы", "👥 По менеджерам", "🗂 Градация", "⏱ Время на этапах", "🤖 AI-аналитика", "📅 План/факт"
])

def fmt_currency(x):
    try: return f"{int(x):,}".replace(","," ")
    except: return "0"

# =========================
# ОБЗОР
# =========================
with tab_over:
    st.subheader("Суммарные показатели")
    # Сделки по дате создания
    ts_deals = ts_with_prev(df.assign(dc=pd.to_datetime(df["DATE_CREATE"])), "dc", "ID", start, end, mode, agg="count", freq_override=agg_freq)

    # Выручка (три воронки, успех)
    target_cats = {CAT_MAIN, CAT_PHYS, CAT_LOW}
    df_succ = df[(df["is_success"]) & (df["cat_norm"].isin(target_cats))].copy()
    df_succ["rev_date"] = df_succ["CLOSEDATE"].fillna(df_succ["DATE_MODIFY"])
    ts_rev_total = ts_with_prev(df_succ.assign(rd=pd.to_datetime(df_succ["rev_date"])), "rd", "OPPORTUNITY", start, end, mode, agg="sum", freq_override=agg_freq)
    per_cat = []
    for cat in [CAT_MAIN, CAT_PHYS, CAT_LOW]:
        part = df_succ[df_succ["cat_norm"]==cat].copy()
        ts = ts_with_prev(part.assign(rd=pd.to_datetime(part["rev_date"])), "rd", "OPPORTUNITY", start, end, mode, agg="sum", freq_override=agg_freq)
        ts["cat"] = cat; per_cat.append(ts)
    ts_rev_by_cat = pd.concat(per_cat, ignore_index=True) if per_cat else pd.DataFrame(columns=["period","value","prev_value","cat"])

    # Среднее здоровье / потенциал
    ts_health = ts_with_prev(df.assign(dm=pd.to_datetime(df["DATE_MODIFY"])), "dm", "health", start, end, mode, agg="mean", freq_override=agg_freq)
    ts_poten  = ts_with_prev(df.assign(dm=pd.to_datetime(df["DATE_MODIFY"])), "dm", "potential", start, end, mode, agg="mean", freq_override=agg_freq)

    def delta_str(cur_prev_df, agg="sum"):
        if cur_prev_df.empty: return "0", "0%"
        cur = cur_prev_df["value"].sum() if agg=="sum" else cur_prev_df["value"].mean()
        pre = cur_prev_df["prev_value"].sum() if agg=="sum" else cur_prev_df["prev_value"].mean()
        diff = cur - pre; pct = (diff / pre * 100.0) if pre else 0.0
        s1 = fmt_currency(cur) if agg=="sum" else f"{cur:.1f}"
        s2 = f"{'+' if diff>=0 else ''}{fmt_currency(diff) if agg=='sum' else f'{diff:.1f}'} ({pct:+.1f}%)"
        return s1, s2

    c1,c2,c3,c4 = st.columns(4)
    val, delta = delta_str(ts_deals, agg="sum"); c1.metric("Сделок", val, delta)
    val, delta = delta_str(ts_rev_total, agg="sum"); c2.metric("Выручка, ₽", val, delta)
    val, delta = delta_str(ts_health, agg="mean"); c3.metric("Среднее здоровье, %", val, delta)
    val, delta = delta_str(ts_poten, agg="mean"); c4.metric("Средний потенциал, %", val, delta)

    if px:
        st.markdown("###### Линия: количество сделок")
        fig_d = px.line(ts_deals, x="period", y="value", markers=True, labels={"value":"Кол-во","period":"Период"})
        fig_d.add_scatter(x=ts_deals["period"], y=ts_deals["prev_value"], mode="lines", name="Пред. период", line=dict(dash="dash"))
        st.plotly_chart(fig_d, use_container_width=True, key="ov_deals_ts")

        st.markdown("###### Линия: выручка по воронкам")
        if not ts_rev_by_cat.empty:
            fig_r = px.line(ts_rev_by_cat, x="period", y="value", color="cat",
                            labels={"value":"Выручка, ₽","period":"Период","cat":"Воронка"},
                            color_discrete_map={CAT_MAIN:"#111111", CAT_PHYS:"#ff7a00", CAT_LOW:"#999999"})
            fig_r.add_scatter(x=ts_rev_total["period"], y=ts_rev_total["prev_value"], name="Сумма (пред.)", line=dict(dash="dash"))
            st.plotly_chart(fig_r, use_container_width=True, key="ov_revenue_bycat")

        st.markdown("###### Линии: среднее здоровье и потенциал")
        colA, colB = st.columns(2)
        with colA:
            fig_h = px.line(ts_health, x="period", y="value", markers=True, labels={"value":"Здоровье %","period":"Период"})
            fig_h.add_scatter(x=ts_health["period"], y=ts_health["prev_value"], mode="lines", name="Пред. период", line=dict(dash="dash"))
            st.plotly_chart(fig_h, use_container_width=True, key="ov_health_ts")
        with colB:
            fig_p = px.line(ts_poten, x="period", y="value", markers=True, labels={"value":"Потенциал %","period":"Период"})
            fig_p.add_scatter(x=ts_poten["period"], y=ts_poten["prev_value"], mode="lines", name="Пред. период", line=dict(dash="dash"))
            st.plotly_chart(fig_p, use_container_width=True, key="ov_potential_ts")

    # Распределение здоровья (воронка 0..100, шаг 5%)
    st.subheader("Распределение здоровья (шаг 5%)")
    bins = list(range(0, 105, 5))
    work = df[(df["DATE_MODIFY"].dt.date.between(start,end))]
    hist = pd.cut(work["health"], bins=bins, right=False).value_counts().sort_index()
    dist = pd.DataFrame({"Диапазон": hist.index.astype(str), "Кол-во": hist.values})
    if px and not dist.empty:
        fig_funnel = px.funnel(dist, y="Диапазон", x="Кол-во", color_discrete_sequence=["#ff7a00"])
        st.plotly_chart(fig_funnel, use_container_width=True, key="ov_health_funnel")
    st.dataframe(dist.rename(columns={"Кол-во":"Кол-во (тек)"}), use_container_width=True)

    # Воронки по этапам (без провалов) + «Провал»
    st.subheader("Воронки по этапам (без провалов) + «Провал» по причинам")
    for cat, title in [(CAT_MAIN, "Основная воронка продаж"), (CAT_PHYS, "Физ.Лица"), (CAT_LOW, "Не приоритетные сделки")]:
        sub = df[(df["cat_norm"]==cat) & (~df["is_fail"])]
        stage = (sub.groupby(["STAGE_ID","stage_name","stage_sort"])["ID"].count()
                 .reset_index().rename(columns={"ID":"Количество"}).sort_values("stage_sort"))
        with st.expander(f"Воронка: {title}"):
            if px and not stage.empty:
                fig_v = px.funnel(stage, y="stage_name", x="Количество", color_discrete_sequence=["#111111" if cat==CAT_MAIN else "#ff7a00"])
                st.plotly_chart(fig_v, use_container_width=True, key=f"ov_funnel_{cat}")
            st.dataframe(stage[["stage_name","Количество"]].rename(columns={"stage_name":"Этап"}), use_container_width=True)

    fails = df[df["is_fail"]]
    fail_by_reason = (fails.groupby(["category","fail_group","stage_name"])["ID"].count()
                      .reset_index().rename(columns={"ID":"Количество"}))
    with st.expander("Провал: причины по группам"):
        if px and not fail_by_reason.empty:
            fig_fail = px.bar(fail_by_reason, x="Количество", y="stage_name", color="fail_group",
                              orientation="h", facet_col="category", height=480,
                              title="Провалы по причинам (группы/воронки)")
            st.plotly_chart(fig_fail, use_container_width=True, key="ov_fails_bar")
        st.dataframe(fail_by_reason.rename(columns={"stage_name":"Причина","category":"Воронка"}), use_container_width=True)

# =========================
# ПРОБЛЕМЫ
# =========================
with tab_prob:
    st.subheader("Метрики проблем")
    problems = {
        "Без задач": int(df["flag_no_tasks"].sum()),
        "Без компании": int(df["flag_no_company"].sum()),
        "Без контакта": int(df["flag_no_contact"].sum()),
        "Застряли": int(df["flag_stuck"].sum()),
        "Проиграны": int(df["is_fail"].sum()),
    }
    a,b,c,d,e = st.columns(5)
    a.metric("Без задач", problems["Без задач"])
    b.metric("Без компании", problems["Без компании"])
    c.metric("Без контакта", problems["Без контакта"])
    d.metric("Застряли", problems["Застряли"])
    e.metric("Проиграны", problems["Проиграны"])

    st.subheader("Распределение проблем по времени")
    if px:
        def build_problem_ts(mask_col):
            tmp = df.assign(dm=pd.to_datetime(df["DATE_MODIFY"]))
            tmp[mask_col] = tmp[mask_col].astype(int)
            return ts_with_prev(tmp, "dm", mask_col, start, end, mode, agg="sum", freq_override=agg_freq)
        lines = []
        for name, col in [("Без задач","flag_no_tasks"),("Без компании","flag_no_company"),
                          ("Без контакта","flag_no_contact"),("Застряли","flag_stuck"),("Проиграны","is_fail")]:
            t = build_problem_ts(col); t["type"]=name; lines.append(t)
        prob_ts = pd.concat(lines, ignore_index=True)
        fig = px.line(prob_ts, x="period", y="value", color="type", labels={"value":"Кол-во","period":"Период","type":"Проблема"})
        base_prev = (prob_ts.groupby("period")["prev_value"].sum().reset_index())
        fig.add_scatter(x=base_prev["period"], y=base_prev["prev_value"], name="Пред. период (сумма)", line=dict(dash="dash"))
        st.plotly_chart(fig, use_container_width=True, key="prob_lines")

    st.subheader("Списки по видам проблем")
    cols = st.columns(5)
    masks = [("Без задач", df["flag_no_tasks"]),("Без контакта", df["flag_no_contact"]),
             ("Без компании", df["flag_no_company"]),("Застряли", df["flag_stuck"]),("Проиграны", df["is_fail"])]
    for (title, mask), box in zip(masks, cols):
        with box:
            st.markdown(f"<div class='card'><div class='title'>{title}</div>", unsafe_allow_html=True)
            st.dataframe(df[mask][["ID","TITLE","manager","stage_name","OPPORTUNITY","health","days_no_activity"]],
                         use_container_width=True, height=260)
            st.markdown("</div>", unsafe_allow_html=True)

# =========================
# ПО МЕНЕДЖЕРАМ
# =========================
with tab_mgr:
    st.subheader("Аналитика по менеджерам (3 воронки и стадии провала)")
    succ = df[(df["is_success"]) & (df["cat_norm"].isin({CAT_MAIN,CAT_PHYS,CAT_LOW}))].copy()
    succ["rev_date"] = succ["CLOSEDATE"].fillna(succ["DATE_MODIFY"])
    won_cnt = succ.groupby("manager")["ID"].count().rename("Выиграно").reset_index()
    won_sum = succ.groupby("manager")["OPPORTUNITY"].sum().rename("Выручка, ₽").reset_index()
    lost_cnt = df[df["is_fail"]].groupby("manager")["ID"].count().rename("Проиграно").reset_index()
    base = df.groupby("manager").agg(Сделок=("ID","count"), СрЗдоровье=("health","mean")).reset_index()
    mgr = base.merge(won_cnt, on="manager", how="left").merge(won_sum, on="manager", how="left").merge(lost_cnt, on="manager", how="left")
    mgr[["Выиграно","Выручка, ₽","Проиграно"]] = mgr[["Выиграно","Выручка, ₽","Проиграно"]].fillna(0)
    mgr["Конверсия в победу, %"] = (mgr["Выиграно"]/mgr["Сделок"]*100).round(1).replace([np.inf,np.nan],0)
    mgr["СрЗдоровье"] = mgr["СрЗдоровье"].round(1)
    st.dataframe(mgr.rename(columns={"manager":"Менеджер"}), use_container_width=True)

    if px and not mgr.empty:
        st.markdown("###### Визуализация по менеджерам")
        fig1 = px.bar(mgr, x="manager", y="Выручка, ₽", color="СрЗдоровье", color_continuous_scale="RdYlGn", labels={"manager":"Менеджер"})
        st.plotly_chart(fig1, use_container_width=True, key="mgr_revenue")
        st.markdown("###### Сделки vs Конверсия")
        fig2 = px.scatter(mgr, x="Сделок", y="Конверсия в победу, %", size="Выручка, ₽", hover_name="manager")
        st.plotly_chart(fig2, use_container_width=True, key="mgr_scatter")

    st.subheader("Конверсия по этапам (читабельно)")
    for cat, title in [(CAT_MAIN,"Основная воронка продаж"), (CAT_PHYS,"Физ.Лица"), (CAT_LOW,"Не приоритетные сделки")]:
        sub = df[df["cat_norm"]==cat]
        st.markdown(f"**{title}**")
        left, right = st.columns(2)
        with left:
            stages = sub[~sub["is_fail"]].groupby(["stage_name","stage_sort"]).size().reset_index(name="Кол-во").sort_values("stage_sort")
            total = stages["Кол-во"].sum() or 1
            stages["Доля, %"] = (stages["Кол-во"]/total*100).round(1)
            st.dataframe(stages[["stage_name","Кол-во","Доля, %"]].rename(columns={"stage_name":"Этап"}), use_container_width=True)
            if px and not stages.empty:
                fig = px.funnel(stages, y="stage_name", x="Кол-во", color_discrete_sequence=["#ff7a00"])
                st.plotly_chart(fig, use_container_width=True, key=f"mgr_conv_funnel_{cat}")
        with right:
            fails = sub[sub["is_fail"]].groupby(["fail_group","stage_name"]).size().reset_index(name="Кол-во")
            if fails.empty:
                st.info("Провалов нет.")
            else:
                st.dataframe(fails.rename(columns={"stage_name":"Причина","fail_group":"Группа"}), use_container_width=True)
                if px:
                    figb = px.bar(fails, x="Кол-во", y="stage_name", color="Группа", orientation="h")
                    st.plotly_chart(figb, use_container_width=True, key=f"mgr_conv_fail_{cat}")

# =========================
# ГРАДАЦИЯ/ВРЕМЯ/AI (как было)
# =========================
with tab_grad:
    st.subheader("Градация сделок (как было)")
    quick = df[(~df["is_fail"]) & (df["PROBABILITY"]>=50) & (df["health"]>=60)].copy()
    work  = df[(~df["is_fail"]) & ~quick.index.isin(quick.index)]
    drop  = df[df["is_fail"]]
    c1,c2,c3 = st.columns(3)
    c1.metric("🟢 Quick Wins", len(quick), fmt_currency(quick["OPPORTUNITY"].sum())+" ₽")
    c2.metric("🟡 Проработка", len(work), fmt_currency(work["OPPORTUNITY"].sum())+" ₽")
    c3.metric("🔴 Stop List", len(drop), fmt_currency(drop["OPPORTUNITY"].sum())+" ₽")
    with st.expander("Списки"):
        st.dataframe(quick[["ID","TITLE","manager","OPPORTUNITY","health","PROBABILITY"]].rename(columns={"OPPORTUNITY":"Сумма"}), use_container_width=True)
        st.dataframe(work[["ID","TITLE","manager","OPPORTUNITY","health","PROBABILITY"]].rename(columns={"OPPORTUNITY":"Сумма"}), use_container_width=True)
        st.dataframe(drop[["ID","TITLE","manager","stage_name","OPPORTUNITY"]].rename(columns={"OPPORTUNITY":"Сумма"}), use_container_width=True)

with tab_time:
    st.subheader("Время на этапах (как было)")
    stage_time = df.groupby("stage_name").agg(СрДней=("days_on_stage","mean"), Мин=("days_on_stage","min"), Макс=("days_on_stage","max")).round(1).reset_index()
    st.dataframe(stage_time.rename(columns={"stage_name":"Этап"}), use_container_width=True)

with tab_ai:
    st.subheader("🤖 AI-аналитика")
    st.caption("Рекомендации как держать здоровье ≥70% + поиск «обходов» (переносы дедлайнов, микро-задачи).")
    def ai_block(mgr_name, g):
        summary = {
            "deals": int(len(g)),
            "avg_health": float(pd.to_numeric(g["health"], errors="coerce").mean() or 0),
            "avg_potential": float(pd.to_numeric(g["potential"], errors="coerce").mean() or 0),
            "no_tasks": int(g["flag_no_tasks"].sum()),
            "stuck": int(g["flag_stuck"].sum()),
            "fails": int(g["is_fail"].sum()),
            "reschedules": int(g["reschedules"].sum()),
            "micro_tasks": int(g["micro_tasks"].sum()),
        }
        if not PERPLEXITY_API_KEY:
            return f"AI недоступен. Сводка: {summary}\n\nРекомендации:\n• Заводите задачи с конкретной датой и результатом.\n• Не переносить дедлайны более 1 раза.\n• Контакт-ритм: не реже 1 раза в 3-5 дней на активных стадиях."
        prompt = f"""
Ты эксперт по CRM. Проанализируй работу менеджера "{mgr_name}".
Данные: {summary}.

1) Сильные стороны.
2) Проблемные зоны.
3) Чек-лист действий в CRM, чтобы здоровье сделок довести/удерживать ≥70% (постановка задач, контроль сроков, контакт-частота, фиксация договорённостей).
4) Есть ли признаки «обхода системы» (переносы дедлайнов, микро-задачи)? Что делать руководителю.
Пиши кратко, деловым стилем.
"""
        try:
            resp = requests.post("https://api.perplexity.ai/chat/completions",
                                 headers={"Authorization": f"Bearer {PERPLEXITY_API_KEY}"},
                                 json={"model":"sonar-pro","messages":[{"role":"user","content":prompt}],
                                       "temperature":0.3,"max_tokens":800},
                                 timeout=30)
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            return f"AI ошибка: {e}"
    for mgr_name, g in df.groupby("manager"):
        with st.expander(f"👤 {mgr_name} ({len(g)} сделок)"):
            st.markdown(ai_block(mgr_name, g))

# =========================
# ПЛАН/ФАКТ
# =========================
with tab_plan:
    st.subheader("Годовой план по выручке — План/Факт/Прогноз")
    year_plan = st.number_input("Целевой план на год, ₽", min_value=0, value=10_000_000, step=100_000, format="%d")
    this_year = datetime.now().year
    succ = df[(df["is_success"]) & (df["cat_norm"].isin({CAT_MAIN,CAT_PHYS,CAT_LOW}))].copy()
    succ["rev_date"] = succ["CLOSEDATE"].fillna(succ["DATE_MODIFY"])
    succ["year"] = pd.to_datetime(succ["rev_date"]).dt.year
    succ_y = succ[succ["year"]==this_year].copy()
    succ_y["month"] = pd.to_datetime(succ_y["rev_date"]).dt.month
    succ_y["quarter"] = ((succ_y["month"]-1)//3 + 1)

    fact_year = float(succ_y["OPPORTUNITY"].sum())
    fact_by_q = succ_y.groupby("quarter")["OPPORTUNITY"].sum().reindex([1,2,3,4], fill_value=0)
    fact_by_m = succ_y.groupby("month")["OPPORTUNITY"].sum().reindex(range(1,13), fill_value=0)

    today = date.today()
    months_passed = today.month
    months_left   = 12 - months_passed + 1
    remaining = max(0.0, year_plan - fact_year)
    need_per_month = (remaining / months_left) if months_left>0 else 0.0
    pct_year = (fact_year / year_plan * 100.0) if year_plan>0 else 0.0

    open_pipe = df[(~df["is_success"]) & (~df["is_fail"])]
    forecast_add = float((open_pipe["OPPORTUNITY"] * open_pipe["PROBABILITY"]/100.0).sum())
    forecast_year = fact_year + forecast_add

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("План (год), ₽", fmt_currency(year_plan))
    c2.metric("Факт YTD, ₽", fmt_currency(fact_year), f"{pct_year:.1f}% выполнено")
    c3.metric("Осталось, ₽", fmt_currency(remaining), f"≈ {fmt_currency(need_per_month)} ₽/мес")
    c4.metric("Прогноз года, ₽", fmt_currency(forecast_year), f"{(forecast_year/year_plan*100 if year_plan else 0):.1f}%")

    plan_q = pd.Series(year_plan/4, index=[1,2,3,4])
    plan_m = pd.Series(year_plan/12, index=range(1,13))
    q_df = pd.DataFrame({"Квартал":[1,2,3,4], "План, ₽":plan_q.values.round(0), "Факт, ₽":fact_by_q.values.round(0)})
    q_df["Выполнение, %"] = (q_df["Факт, ₽"]/q_df["План, ₽"]*100).replace([np.inf,np.nan],0).round(1)
    q_df["Осталось, ₽"] = (q_df["План, ₽"] - q_df["Факт, ₽"]).round(0)

    m_df = pd.DataFrame({"Месяц":range(1,13), "План, ₽":plan_m.values.round(0), "Факт, ₽":fact_by_m.values.round(0)})
    m_df["Выполнение, %"] = (m_df["Факт, ₽"]/m_df["План, ₽"]*100).replace([np.inf,np.nan],0).round(1)
    m_df["Осталось, ₽"] = (m_df["План, ₽"] - m_df["Факт, ₽"]).round(0)

    st.markdown("###### Кварталы — план/факт")
    st.dataframe(q_df, use_container_width=True)
    st.markdown("###### Месяцы — план/факт")
    st.dataframe(m_df, use_container_width=True)

    if px:
        st.markdown("###### График: Факт vs План (месяцы)")
        fig_plan = px.line(m_df, x="Месяц", y="Факт, ₽", markers=True)
        fig_plan.add_scatter(x=m_df["Месяц"], y=m_df["План, ₽"], name="План", line=dict(dash="dash"))
        st.plotly_chart(fig_plan, use_container_width=True, key="plan_fact_months")

st.markdown("---")
st.caption("БУРМАШ · CRM Дэшборд v5.1")
