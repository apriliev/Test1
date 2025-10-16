# -*- coding: utf-8 -*-
"""
БУРМАШ · CRM Дэшборд (v3.2 — бело/оранж/чёрная тема, логотип, адаптивный порядок этапов, sales-only)
"""

import os
import time
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st

try:
    import plotly.express as px
except Exception:
    px = None

# ------------------------
# UI
# ------------------------
st.set_page_config(page_title="БУРМАШ · CRM Дэшборд", page_icon="🟧", layout="wide")

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
.headerbar img{ height:38px; }
</style>
"""
st.markdown(THEME_CSS, unsafe_allow_html=True)

# ------------------------
# АВТОРИЗАЦИЯ (admin/admin123)
# ------------------------
def check_password():
    def password_entered():
        st.session_state["password_correct"] = (
            st.session_state.get("username") == "admin"
            and st.session_state.get("password") == "admin123"
        )
        st.session_state.pop("password", None)

    if st.secrets.get("DISABLE_AUTH", False):
        st.session_state["password_correct"] = True

    if not st.session_state.get("password_correct", False):
        st.markdown("### 🔐 Вход — БУРМАШ")
        st.text_input("Логин", key="username")
        st.text_input("Пароль", type="password", key="password", on_change=password_entered)
        st.stop()

check_password()
with st.sidebar:
    if st.button("Выйти"):
        st.session_state["password_correct"] = False
        st.rerun()

# ------------------------
# Секреты / окружение
# ------------------------
def get_secret(name, default=None):
    if name in st.secrets:
        return st.secrets[name]
    return os.getenv(name, default)

BITRIX24_WEBHOOK = (get_secret("BITRIX24_WEBHOOK", "") or "").strip()
LOGO_PATH = "/mnt/data/burmash-logo-rgb-01.png"

# ------------------------
# Bitrix24 helpers
# ------------------------
def _bx_get(method, params=None, pause=0.4):
    url = BITRIX24_WEBHOOK.rstrip("/") + f"/{method}.json"
    out, start = [], 0
    params = dict(params or {})
    while True:
        params["start"] = start
        import requests
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
def bx_get_deals(date_from=None, date_to=None, limit=1000):
    params = {"select[]":[
        "ID","TITLE","STAGE_ID","OPPORTUNITY","ASSIGNED_BY_ID","COMPANY_ID","CONTACT_ID",
        "PROBABILITY","DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME","CATEGORY_ID"
    ]}
    if date_from: params["filter[>=DATE_CREATE]"] = date_from
    if date_to:   params["filter[<=DATE_CREATE]"] = date_to
    deals = _bx_get("crm.deal.list", params)
    return deals[:limit]

@st.cache_data(ttl=300)
def bx_get_departments():
    try:
        return _bx_get("department.get", {})
    except Exception:
        return []

@st.cache_data(ttl=300)
def bx_get_users_full():
    users = _bx_get("user.get", {})
    out = {}
    for u in users:
        depts = u.get("UF_DEPARTMENT") or []
        if isinstance(depts, str):
            depts = [int(x) for x in depts.split(",") if x]
        out[int(u["ID"])] = {
            "name": ((u.get("NAME","")+" "+u.get("LAST_NAME","")).strip() or u.get("LOGIN","")).strip(),
            "depts": list(map(int, depts)) if depts else [],
            "active": (u.get("ACTIVE","Y")=="Y")
        }
    return out

@st.cache_data(ttl=300)
def bx_get_open_activities_for_deal_ids(deal_ids):
    out = {}
    if not deal_ids: return out
    for chunk in np.array_split(list(map(int, deal_ids)), max(1, len(deal_ids)//40 + 1)):
        params = {"filter[OWNER_TYPE_ID]":2,"filter[OWNER_ID]":",".join(map(str,chunk)),"filter[COMPLETED]":"N"}
        acts = _bx_get("crm.activity.list", params)
        for a in acts:
            out.setdefault(int(a["OWNER_ID"]), []).append(a)
    return out

# ---- ЭТАПЫ/СТАТУСЫ: адаптивный порядок и названия ----
@st.cache_data(ttl=600)
def bx_get_stage_map(stage_ids):
    """
    Возвращает:
    - sort_map: {STAGE_ID -> SORT:int}
    - name_map: {STAGE_ID -> NAME:str}
    Пытается тянуть crm.status.list по нужным категориям.
    """
    sort_map, name_map = {}, {}

    if not BITRIX24_WEBHOOK or not stage_ids:
        return sort_map, name_map

    # категории по префиксу "C{ID}:"
    cats = set()
    for sid in stage_ids:
        if isinstance(sid, str) and sid.startswith("C"):
            try:
                cid = int(sid.split(":")[0][1:])
                cats.add(cid)
            except Exception:
                pass

    # базовая (дефолтная) воронка
    try:
        base = _bx_get("crm.status.list", {"filter[ENTITY_ID]":"DEAL_STAGE"})
        for s in base:
            sort_map[s.get("STATUS_ID")] = int(s.get("SORT", 5000))
            name_map[s.get("STATUS_ID")] = s.get("NAME") or s.get("STATUS_ID")
    except Exception:
        pass

    # по конкретным категориям
    for cid in cats:
        try:
            resp = _bx_get("crm.status.list", {"filter[ENTITY_ID]": f"DEAL_STAGE_{cid}"})
            for s in resp:
                sort_map[s.get("STATUS_ID")] = int(s.get("SORT", 5000))
                name_map[s.get("STATUS_ID")] = s.get("NAME") or s.get("STATUS_ID")
        except Exception:
            continue

    return sort_map, name_map

# ------------------------
# Даты/подсчёты
# ------------------------
def to_dt(x):
    try:
        ts = pd.to_datetime(x, utc=True, errors="coerce")
        if pd.isna(ts): return pd.NaT
        return ts.tz_convert(None)
    except Exception:
        return pd.NaT

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
        d_work  = days_between(now, create_dt) or 0
        d_noact = days_between(now, last) or 0
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
            "ID": int(r["ID"]), "TITLE": r.get("TITLE",""),
            "ASSIGNED_BY_ID": int(r.get("ASSIGNED_BY_ID") or 0),
            "STAGE_ID": r.get("STAGE_ID",""),
            "CATEGORY_ID": r.get("CATEGORY_ID"),
            "OPPORTUNITY": opp, "PROBABILITY": prob,
            "DATE_CREATE": create_dt, "DATE_MODIFY": to_dt(r.get("DATE_MODIFY")), "LAST_ACTIVITY_TIME": last,
            "days_in_work": d_work, "days_no_activity": d_noact,
            "health": max(0, min(100, int(score))), "potential": max(0, min(100, int(potential))),
            "flag_no_company": flags["no_company"], "flag_no_contact": flags["no_contact"],
            "flag_no_tasks": flags["no_tasks"], "flag_stuck": flags["stuck"], "flag_lost": flags["lost"],
        })
    return pd.DataFrame(rows)

def deal_recommendations(row):
    recs = []
    if row["flag_lost"]:
        return ["Проверьте причину проигрыша, при шансе — верните сделку в работу (альтернатива/рассрочка)."]
    if row["flag_no_tasks"]:   recs.append("Поставьте задачу на следующий шаг (дата + комментарий).")
    if row["flag_stuck"]:      recs.append("Нет активности — звонок сегодня + письмо-резюме. Обновите этап.")
    if row["flag_no_contact"]: recs.append("Добавьте контакт ЛПР (ФИО, телефон/email).")
    if row["flag_no_company"]: recs.append("Заполните карточку компании (ИНН, сайт, отрасль).")
    if row["health"] < 60 and row["potential"] >= 50:
        recs.append("Высокий потенциал при низком здоровье — назначьте встречу/демо, ускорьте КП/ТЗ.")
    if row["OPPORTUNITY"] > 0 and row["PROBABILITY"] < 40:
        recs.append("Есть сумма, но низкая вероятность — уточните бюджет/сроки/ЛПР и обновите вероятность.")
    if row["days_in_work"] > 20 and row["PROBABILITY"] < 30:
        recs.append("Долгое ведение — поднимите уровень договорённостей или переформатируйте план.")
    if not recs:
        recs.append("Продолжайте по этапу: подтвердите следующую встречу и зафиксируйте договорённости.")
    return recs

def activity_series(row, points=60):
    end = to_dt(pd.Timestamp.utcnow())
    start = row["DATE_CREATE"] if pd.notna(row["DATE_CREATE"]) else end - pd.Timedelta(days=30)
    start = to_dt(start)
    if not pd.notna(start) or start >= end:
        start = end - pd.Timedelta(days=1)
    idx = pd.date_range(start, end, periods=max(2,int(points)))
    y = np.random.default_rng(int(row["ID"])).normal(0.1, 0.02, size=len(idx)).clip(0,1)
    near_start = np.argmin(np.abs(idx - start))
    last = row["LAST_ACTIVITY_TIME"] if pd.notna(row["LAST_ACTIVITY_TIME"]) else end
    near_last = np.argmin(np.abs(idx - to_dt(last)))
    for i in range(len(idx)):
        y[i] += 0.4 * math.exp(-abs(i-near_start)/6)
        y[i] += 0.8 * math.exp(-abs(i-near_last)/4)
    return pd.DataFrame({"ts": idx, "activity": y})

# ------------------------
# Сайдбар: фильтры
# ------------------------
st.sidebar.title("Фильтры")
date_from  = st.sidebar.date_input("С какой даты", datetime.now().date() - timedelta(days=30))
date_to    = st.sidebar.date_input("По какую дату", datetime.now().date())
stuck_days = st.sidebar.slider("Нет активности ≥ (дней)", 2, 21, 5)
limit      = st.sidebar.slider("Лимит сделок (API)", 50, 3000, 600, step=50)

uploaded_offline = None
if not BITRIX24_WEBHOOK:
    st.sidebar.warning("Нет BITRIX24_WEBHOOK — офлайн-режим (загрузите CSV/XLSX).")
    uploaded_offline = st.sidebar.file_uploader("CSV/XLSX со сделками", type=["csv","xlsx"])

# ------------------------
# Загрузка данных
# ------------------------
with st.spinner("Загружаю данные…"):
    if BITRIX24_WEBHOOK:
        deals_raw = bx_get_deals(str(date_from), str(date_to), limit=limit)
        if not deals_raw:
            st.error("Сделок не найдено за выбранный период."); st.stop()
        df_raw = pd.DataFrame(deals_raw)
        df_raw["OPPORTUNITY"] = pd.to_numeric(df_raw.get("OPPORTUNITY"), errors="coerce").fillna(0.0)

        users_full = bx_get_users_full()
        departments = bx_get_departments()
        # отделы продаж по названию
        sales_depts = [d for d in departments if "продаж" in (d.get("NAME","").lower())]
        sales_dept_ids = {int(d["ID"]) for d in sales_depts}
        # флаги сайдбара
        default_sales_only = bool(sales_dept_ids)
        show_sales_only = st.sidebar.checkbox("Только сотрудники отдела продаж", value=default_sales_only, disabled=not bool(users_full))
        selected_depts = st.sidebar.multiselect(
            "Отделы (фильтр по сотрудникам)",
            options=[(int(d["ID"]), d["NAME"]) for d in departments],
            default=[(int(d["ID"]), d["NAME"]) for d in sales_depts],
            format_func=lambda t: t[1] if isinstance(t, tuple) else str(t)
        ) if departments else []
        selected_dept_ids = {t[0] for t in selected_depts} if selected_depts else sales_dept_ids

        users_map = {uid: users_full[uid]["name"] for uid in users_full}
        open_tasks_map = bx_get_open_activities_for_deal_ids(df_raw["ID"].tolist())

    else:
        if not uploaded_offline:
            st.info("Загрузите CSV/XLSX со столбцами: ID, TITLE, STAGE_ID, OPPORTUNITY, ASSIGNED_BY_ID, COMPANY_ID, CONTACT_ID, PROBABILITY, DATE_CREATE, DATE_MODIFY, LAST_ACTIVITY_TIME.")
            st.stop()
        if uploaded_offline.name.lower().endswith(".csv"):
            df_raw = pd.read_csv(uploaded_offline)
        else:
            df_raw = pd.read_excel(uploaded_offline)
        df_raw.columns = [c.strip() for c in df_raw.columns]
        must = ["ID","TITLE","STAGE_ID","OPPORTUNITY","ASSIGNED_BY_ID","COMPANY_ID","CONTACT_ID","PROBABILITY","DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME"]
        missing = [c for c in must if c not in df_raw.columns]
        if missing: st.error(f"Не хватает колонок: {missing}"); st.stop()
        df_raw["OPPORTUNITY"] = pd.to_numeric(df_raw["OPPORTUNITY"], errors="coerce").fillna(0.0)
        users_map = {int(i): str(i) for i in pd.to_numeric(df_raw["ASSIGNED_BY_ID"], errors="coerce").fillna(0).astype(int).unique()}
        open_tasks_map = {}
        show_sales_only = False
        selected_dept_ids = set()
        users_full = {}

    # расчёт метрик
    df_scores = compute_health_scores(df_raw, open_tasks_map, stuck_days=stuck_days)

    # фильтр сотрудников отдела продаж
    if BITRIX24_WEBHOOK and show_sales_only and selected_dept_ids:
        keep_ids = [uid for uid, info in users_full.items() if set(info["depts"]) & selected_dept_ids]
        df_scores = df_scores[df_scores["ASSIGNED_BY_ID"].isin(keep_ids)]

    # карта этапов (порядок/имена)
    stage_ids = df_scores["STAGE_ID"].dropna().unique().tolist()
    sort_map, name_map = bx_get_stage_map(stage_ids)

    # запасной порядок — если не удалось получить из Битрикс
    FALLBACK_ORDER = [
        "NEW","NEW_LEAD","PREPARATION","PREPAYMENT_INVOICE","EXECUTING","FINAL_INVOICE","WON","LOSE","LOSE_REASON"
    ]
    def fallback_sort(sid):
        sid = str(sid or "")
        # убираем префикс "C*:" если есть
        if ":" in sid: sid_short = sid.split(":")[1]
        else: sid_short = sid
        if sid_short in FALLBACK_ORDER:
            return FALLBACK_ORDER.index(sid_short)*100
        # оставшиеся — по алфавиту после основных
        return 10000 + hash(sid_short)%1000

    df_scores["stage_sort"] = df_scores["STAGE_ID"].map(lambda s: sort_map.get(str(s), fallback_sort(s)))
    df_scores["stage_name"] = df_scores["STAGE_ID"].map(lambda s: name_map.get(str(s), str(s)))

    df_scores["manager"] = df_scores["ASSIGNED_BY_ID"].map(users_map).fillna("Неизвестно")

# глобальный фильтр по менеджерам
manager_options = sorted(df_scores["manager"].unique())
selected_managers = st.sidebar.multiselect("Менеджеры", manager_options, default=[])
view_df = df_scores[df_scores["manager"].isin(selected_managers)] if selected_managers else df_scores.copy()

# ------------------------
# Шапка + логотип
# ------------------------
col_logo, col_title = st.columns([1,5], vertical_alignment="center")
with col_logo:
    try:
        st.image(LOGO_PATH, use_column_width=False)
    except Exception:
        st.markdown("<div class='pill'>БУРМАШ</div>", unsafe_allow_html=True)
with col_title:
    st.markdown("<div class='headerbar'><div class='pill'>Контроль отдела продаж</div></div>", unsafe_allow_html=True)
    st.caption("Автоаудит · Пульс воронки · Зоны менеджеров · Карточки · Отчёт по сделке")

# верхние метрики
c1,c2,c3,c4,c5 = st.columns(5, gap="small")
with c1: st.metric("Всего сделок", int(view_df.shape[0]))
with c2: st.metric("Объём, ₽", f"{int(view_df['OPPORTUNITY'].sum()):,}".replace(","," "))
with c3: st.metric("Средний чек, ₽", f"{int(view_df['OPPORTUNITY'].replace(0,np.nan).mean() or 0):,}".replace(","," "))
with c4: st.metric("Средн. здоровье", f"{view_df['health'].mean():.0f}%")
with c5: st.metric("Суммарный потенциал", int(view_df["potential"].sum()))

# ------------------------
# Вкладки
# ------------------------
tab_pulse, tab_audit, tab_managers, tab_cards, tab_deal = st.tabs([
    "⛵ Пульс воронки", "🚧 Аудит", "👥 Менеджеры", "🗂 Карточки", "📄 Отчёт по сделке"
])

# --- ПУЛЬС ВОРОНКИ
with tab_pulse:
    st.markdown("##### Воронка этапов (адаптивный порядок)")
    if px is None:
        st.info("Plotly недоступен.")
    else:
        metric_kind = st.radio("Показатель", ["Количество", "Сумма, ₽"], horizontal=True, key="metric_kind")
        funnel_df = (
            view_df.groupby(["STAGE_ID","stage_name","stage_sort"])
            .agg(Количество=("ID","count"), Сумма=("OPPORTUNITY","sum"))
            .reset_index()
            .sort_values("stage_sort")
        )
        if metric_kind == "Количество":
            fig = px.funnel(funnel_df, x="Количество", y="stage_name", color_discrete_sequence=["#ff7a00"])
        else:
            fig = px.funnel(funnel_df, x="Сумма", y="stage_name", color_discrete_sequence=["#ff7a00"])
        fig.update_layout(margin=dict(l=10,r=10,t=10,b=10), height=420)
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("##### Тренд новых сделок по датам создания")
    if px:
        trend = view_df.copy()
        trend["date"] = pd.to_datetime(trend["DATE_CREATE"]).dt.date
        trend = trend.groupby("date").agg(Количество=("ID","count"), Сумма=("OPPORTUNITY","sum")).reset_index()
        tcol1, tcol2 = st.columns(2, gap="large")
        with tcol1:
            fig1 = px.line(trend, x="date", y="Количество", markers=True, color_discrete_sequence=["#ff7a00"])
            fig1.update_layout(margin=dict(l=10,r=10,t=10,b=10), height=280)
            st.plotly_chart(fig1, use_container_width=True)
        with tcol2:
            fig2 = px.area(trend, x="date", y="Сумма", color_discrete_sequence=["#111111"])
            fig2.update_layout(margin=dict(l=10,r=10,t=10,b=10), height=280)
            st.plotly_chart(fig2, use_container_width=True)

    st.markdown("##### Лента изменений (последние)")
    st.dataframe(
        view_df.sort_values("DATE_MODIFY", ascending=False)[
            ["ID","TITLE","manager","stage_name","OPPORTUNITY","health","potential","DATE_MODIFY"]
        ].head(200),
        height=360
    )

# --- АУДИТ
with tab_audit:
    st.markdown("##### Проблемные зоны")
    kpis = {
        "Без задач": int(view_df["flag_no_tasks"].sum()),
        "Без контактов": int(view_df["flag_no_contact"].sum()),
        "Без компаний": int(view_df["flag_no_company"].sum()),
        "Застряли": int(view_df["flag_stuck"].sum()),
        "Потерянные": int(view_df["flag_lost"].sum()),
    }
    a,b,c,d,e = st.columns(5)
    a.metric("Без задач", kpis["Без задач"])
    b.metric("Без контактов", kpis["Без контактов"])
    c.metric("Без компаний", kpis["Без компаний"])
    d.metric("Застряли", kpis["Застряли"])
    e.metric("Потерянные", kpis["Потерянные"])

    if px:
        audit_df = pd.DataFrame({"Проблема": list(kpis.keys()), "Количество": list(kpis.values())}).sort_values("Количество", ascending=False)
        fig = px.bar(audit_df, x="Количество", y="Проблема", orientation="h",
                     color="Количество", color_continuous_scale=["#ffe8d6","#ff7a00"])
        fig.update_layout(coloraxis_showscale=False, margin=dict(l=10,r=10,t=10,b=10), height=320)
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("##### Списки по категориям")
    cols = st.columns(5, gap="small")
    lists = [
        ("Без задач", view_df["flag_no_tasks"]),
        ("Без контактов", view_df["flag_no_contact"]),
        ("Без компаний", view_df["flag_no_company"]),
        ("Застряли", view_df["flag_stuck"]),
        ("Потерянные", view_df["flag_lost"]),
    ]
    for (title, mask), holder in zip(lists, cols):
        with holder:
            st.markdown("<div class='card'><div class='title'>%s</div>" % title, unsafe_allow_html=True)
            st.dataframe(
                view_df[mask][["ID","TITLE","manager","stage_name","OPPORTUNITY","health","days_no_activity"]].head(80),
                height=260
            )
            st.markdown("</div>", unsafe_allow_html=True)

# --- МЕНЕДЖЕРЫ
with tab_managers:
    st.markdown("##### Квадрант: здоровье × без задач (размер — сумма)")
    if px:
        quad = view_df.groupby("manager").agg(
            health_avg=("health","mean"),
            no_tasks=("flag_no_tasks","sum"),
            opp_sum=("OPPORTUNITY","sum"),
            deals=("ID","count")
        ).reset_index()
        fig = px.scatter(quad, x="health_avg", y="no_tasks", size="opp_sum",
                         hover_data=["deals","manager"], color="health_avg",
                         color_continuous_scale=["#ffe8d6","#ff7a00","#111111"])
        fig.update_layout(coloraxis_showscale=False, margin=dict(l=10,r=10,t=10,b=10), height=420)
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("##### Рейтинг по среднему здоровью")
    if px:
        rating = view_df.groupby("manager").agg(health_avg=("health","mean"), deals=("ID","count")).reset_index()
        rating = rating.sort_values("health_avg", ascending=True)
        fig = px.bar(rating, x="health_avg", y="manager", orientation="h", text="deals",
                     color="health_avg", color_continuous_scale=["#ffe8d6","#ff7a00"])
        fig.update_traces(texttemplate="сделок: %{text}", textposition="outside", cliponaxis=False)
        fig.update_layout(coloraxis_showscale=False, margin=dict(l=10,r=10,t=10,b=10), height=520)
        st.plotly_chart(fig, use_container_width=True)

# --- КАРТОЧКИ
with tab_cards:
    st.markdown("##### Приоритетные сделки (сначала слабые по здоровью)")
    pick = view_df.sort_values(["health","potential","OPPORTUNITY"], ascending=[True,False,False]).head(30)
    cols = st.columns(3, gap="medium")
    for i, (_, row) in enumerate(pick.iterrows()):
        with cols[i % 3]:
            badge_cls = "bad" if row["health"] < 60 else ("good" if row["health"]>=80 else "warn")
            risks_list = [k.replace("flag_","").replace("_"," ") for k in
                          ["flag_no_tasks","flag_no_company","flag_no_contact","flag_stuck"] if row[k]]
            recs = deal_recommendations(row)
            st.markdown(f"""
            <div class="card">
              <div class="title">{row['TITLE']}</div>
              <div class="subtle">ID {row['ID']} • {row['manager']}</div>
              <hr/>
              <span class="badge {badge_cls}">Здоровье: <b>{row['health']}%</b></span>
              <span class="badge">Потенциал: <b>{row['potential']}%</b></span>
              <span class="badge">Сумма: <b>{int(row['OPPORTUNITY']):,} ₽</b></span>
              <span class="badge">Этап: <b>{row['stage_name']}</b></span>
              <span class="badge">Дней в работе: <b>{row['days_in_work']}</b></span>
              <span class="badge">Без активности: <b>{row['days_no_activity']} дн</b></span>
              <hr/>
              <div class="subtle">⚠️ Риски: {", ".join(risks_list) or "нет"}</div>
              <div class="subtle">✅ Следующие шаги:<br/>• {"<br/>• ".join(recs)}</div>
            </div>
            """, unsafe_allow_html=True)

# --- ОТЧЁТ ПО СДЕЛКЕ
with tab_deal:
    st.markdown("##### Подробный отчёт")
    options = view_df.sort_values("DATE_MODIFY", ascending=False)
    if options.empty:
        st.info("Нет сделок по текущим фильтрам."); st.stop()
    label_map = {int(r.ID): f"[{int(r.ID)}] {r.TITLE} — {r.manager}" for r in options[["ID","TITLE","manager"]].itertuples(index=False)}
    chosen_id = st.selectbox("Сделка", list(label_map.keys()), format_func=lambda x: label_map[x])
    deal = view_df[view_df["ID"]==chosen_id].iloc[0]

    # верхняя строка
    a,b,c,d = st.columns([1.4,1,1,1], vertical_alignment="center")
    with a:
        st.markdown(f"<div class='title'>{deal['TITLE']}</div>", unsafe_allow_html=True)
        st.caption(f"Компания: БУРМАШ • Ответственный: {deal['manager']} • Этап: {deal['stage_name']}")
    with b: st.markdown(f"<div class='score'>{deal['potential']}</div><div class='kpi-caption'>Потенциал</div>", unsafe_allow_html=True)
    with c: st.markdown(f"<div class='score'>{deal['health']}</div><div class='kpi-caption'>Здоровье</div>", unsafe_allow_html=True)
    with d: st.markdown(f"<div class='kpi'>{int(deal['OPPORTUNITY'])}</div><div class='kpi-caption'>Сумма, ₽</div>", unsafe_allow_html=True)

    # карточки
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("<div class='title'>Параметры</div>", unsafe_allow_html=True)
        st.markdown(f"""
        <div class="grid2">
          <div class="card"><div class="title">Сумма</div><div class="kpi">{int(deal['OPPORTUNITY'])}</div><div class="kpi-caption">вероятность {int(deal['PROBABILITY'])}%</div></div>
          <div class="card"><div class="title">Сроки</div><div class="kpi">{deal['days_in_work']}</div><div class="kpi-caption">без активности {deal['days_no_activity']} дн</div></div>
        </div>
        """, unsafe_allow_html=True)

        fin = ("Бюджет не подтверждён, сумма в сделке = 0." if deal["OPPORTUNITY"]<=0
               else ("Бюджет обсуждается, вероятность низкая — требуется подтверждение ЛПР и КП."
                     if deal["PROBABILITY"]<40 else
                     "Бюджет ориентировочно подтверждён, требуется финализация условий."))
        lpr = "Контакт есть" if not deal["flag_no_contact"] else "ЛПР не указан — подтвердите ФИО и роль."
        need = "Интерес подтверждён; уточните критерии успеха и сроки." if deal["PROBABILITY"]>=30 else "Потребность не зафиксирована — сформулируйте задачу и результат."
        timebox = ("Нет задачи на следующий шаг — согласуйте дату контакта." if deal["flag_no_tasks"]
                   else ("Просрочка активности — сделайте контакт и обновите этап." if deal["flag_stuck"]
                         else "Сроки контролируются задачами."))
        main_task = "Назначить встречу/демо и прислать КП" if deal["PROBABILITY"]<50 else "Согласовать условия и направить договор/счёт"

        st.markdown(f"""
        <div class="grid2">
          <div class="card"><div class="title">Финансовая готовность</div><div class="subtle">{fin}</div></div>
          <div class="card"><div class="title">Полномочие принятия решения</div><div class="subtle">{lpr}</div></div>
        </div>
        <div class="grid2">
          <div class="card"><div class="title">Потребность и наш фокус</div><div class="subtle">{need}</div></div>
          <div class="card"><div class="title">Сроки и готовность к покупке</div><div class="subtle">{timebox}</div></div>
        </div>
        <div class="card"><div class="title">Задача</div><div class="subtle">Менеджеру: {main_task}.</div></div>
        """, unsafe_allow_html=True)

    with right:
        st.markdown("<div class='title'>Динамика и итог</div>", unsafe_allow_html=True)
        if px:
            line = activity_series(deal)
            fig = px.line(line, x="ts", y="activity", markers=True, color_discrete_sequence=["#ff7a00"])
            fig.update_yaxes(visible=False)
            fig.update_layout(margin=dict(l=10,r=10,t=10,b=10), height=240)
            st.markdown("<div class='card'>", unsafe_allow_html=True)
            st.plotly_chart(fig, use_container_width=True)
            st.markdown("</div>", unsafe_allow_html=True)

        risks_list = [name for name,flag in {
            "без задач": deal["flag_no_tasks"], "без контактов": deal["flag_no_contact"],
            "без компании": deal["flag_no_company"], "застряла": deal["flag_stuck"]
        }.items() if flag]
        st.markdown(f"""
        <div class="card"><div class="title">Итоги работы</div>
        <div class="subtle">Этап: {deal['stage_name'] or '—'}. Последняя активность: {str(deal['LAST_ACTIVITY_TIME'])[:19]}.<br/>
        Риски: {", ".join(risks_list) if risks_list else "существенных рисков не выявлено"}.</div></div>
        """, unsafe_allow_html=True)
        recs = deal_recommendations(deal)
        st.markdown(f"<div class='card'><div class='title'>План действий</div><div class='subtle'>• {'<br/>• '.join(recs)}</div></div>", unsafe_allow_html=True)
