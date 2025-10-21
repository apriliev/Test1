# -*- coding: utf-8 -*-
"""
БУРМАШ · CRM Дэшборд (v4.3)
— Надёжная авторизация (форма, не сбрасывается при ререндерах)
— Уникальные key у всех plotly_chart (нет StreamlitDuplicateElementId)
— Подсказчик РОПу: Quick Wins / Stop List + ETA
— Адаптивный порядок этапов из Bitrix (crm.status.list), человеко-читаемые названия
— Опция «Только отдел продаж» (по названию отделов с “продаж” + ручной выбор)
— Бело/оранжево/чёрная тема, без логотипа и без файловых выгрузок
"""

import os
import time
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st
import requests

# =========================
# Графики
# =========================
try:
    import plotly.express as px
except Exception:
    px = None

# =========================
# UI / ТЕМА
# =========================
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
</style>
"""
st.markdown(THEME_CSS, unsafe_allow_html=True)

# =========================
# АВТОРИЗАЦИЯ (форма)
# =========================
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

# =========================
# Секреты / окружение
# =========================
def get_secret(name, default=None):
    if name in st.secrets:
        return st.secrets[name]
    return os.getenv(name, default)

BITRIX24_WEBHOOK = (get_secret("BITRIX24_WEBHOOK", "") or "").strip()
PERPLEXITY_API_KEY = (get_secret("PERPLEXITY_API_KEY", "") or "").strip()  # опционально; если нет — AI-вкладка покажет сообщение

# =========================
# Bitrix24 helpers
# =========================
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
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 50:
            break
        start += 50
        time.sleep(pause)
    return out

@st.cache_data(ttl=300)
def bx_get_deals(date_from=None, date_to=None, limit=1000):
    params = {"select[]":[
        "ID","TITLE","STAGE_ID","OPPORTUNITY","ASSIGNED_BY_ID","COMPANY_ID","CONTACT_ID",
        "PROBABILITY","DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME","CATEGORY_ID","BEGINDATE"
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
    if not deal_ids:
        return out
    for chunk in np.array_split(list(map(int, deal_ids)), max(1, len(deal_ids)//40 + 1)):
        params = {"filter[OWNER_TYPE_ID]":2,"filter[OWNER_ID]":",".join(map(str,chunk)),"filter[COMPLETED]":"N"}
        acts = _bx_get("crm.activity.list", params)
        for a in acts:
            out.setdefault(int(a["OWNER_ID"]), []).append(a)
    return out

@st.cache_data(ttl=600)
def bx_get_stage_map(stage_ids):
    """Возвращает (sort_map, name_map) для этапов из всех нужных воронок."""
    sort_map, name_map = {}, {}
    if not BITRIX24_WEBHOOK or not stage_ids:
        return sort_map, name_map

    # категории из префикса C{ID}:STAGE
    cats = set()
    for sid in stage_ids:
        if isinstance(sid, str) and sid.startswith("C"):
            try:
                cats.add(int(sid.split(":")[0][1:]))
            except Exception:
                pass

    # Базовая воронка
    try:
        base = _bx_get("crm.status.list", {"filter[ENTITY_ID]":"DEAL_STAGE"})
        for s in base:
            sort_map[s.get("STATUS_ID")] = int(s.get("SORT", 5000))
            name_map[s.get("STATUS_ID")] = s.get("NAME") or s.get("STATUS_ID")
    except Exception:
        pass

    # Конкретные категории
    for cid in cats:
        try:
            resp = _bx_get("crm.status.list", {"filter[ENTITY_ID]": f"DEAL_STAGE_{cid}"})
            for s in resp:
                sort_map[s.get("STATUS_ID")] = int(s.get("SORT", 5000))
                name_map[s.get("STATUS_ID")] = s.get("NAME") or s.get("STATUS_ID")
        except Exception:
            continue

    return sort_map, name_map

@st.cache_data(ttl=600)
def bx_get_categories():
    try:
        cats = _bx_get("crm.category.list", {})
        return {int(c["ID"]): c.get("NAME","Воронка") for c in cats}
    except Exception:
        return {}

# =========================
# Утилиты
# =========================
def to_dt(x):
    try:
        ts = pd.to_datetime(x, utc=True, errors="coerce")
        if pd.isna(ts): return pd.NaT
        return ts.tz_convert(None)  # сделать naive UTC
    except Exception:
        return pd.NaT

def days_between(later, earlier):
    a, b = to_dt(later), to_dt(earlier)
    if pd.isna(a) or pd.isna(b):
        return None
    return max(0, int((a - b) / pd.Timedelta(days=1)))

# =========================
# Скоринги
# =========================
def compute_health_scores(df, open_tasks_map, stuck_days=5):
    now = to_dt(pd.Timestamp.utcnow())
    rows = []
    for _, r in df.iterrows():
        create_dt = to_dt(r.get("DATE_CREATE"))
        last = to_dt(r.get("LAST_ACTIVITY_TIME")) or to_dt(r.get("DATE_MODIFY")) or create_dt
        begin_dt = to_dt(r.get("BEGINDATE")) or create_dt

        d_work  = days_between(now, create_dt) or 0
        d_noact = days_between(now, last) or 0
        d_in_stage = days_between(now, begin_dt) or 0

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
            "ID сделки": int(r["ID"]),
            "Название": r.get("TITLE",""),
            "Менеджер ID": int(r.get("ASSIGNED_BY_ID") or 0),
            "Этап ID": r.get("STAGE_ID",""),
            "Воронка ID": r.get("CATEGORY_ID"),
            "Сумма": opp,
            "Вероятность": prob,
            "Дата создания": create_dt,
            "Дата изменения": to_dt(r.get("DATE_MODIFY")),
            "Последняя активность": last,
            "Начало этапа": begin_dt,
            "Дней в работе": d_work,
            "Дней без активности": d_noact,
            "Дней на этапе": d_in_stage,
            "Здоровье": max(0, min(100, int(score))),
            "Потенциал": max(0, min(100, int(potential))),
            "Нет компании": flags["no_company"],
            "Нет контакта": flags["no_contact"],
            "Нет задач": flags["no_tasks"],
            "Застряла": flags["stuck"],
            "Проиграна": flags["lost"],
        })
    return pd.DataFrame(rows)

def focus_scores(df, horizon_days=14, min_prob=50):
    """Оценки для подсказчика РОПу."""
    if df.empty:
        return df.assign(**{"Скор быстрой победы":0.0, "ETA дней":np.nan, "Скор отказа":0.0})

    eps = 1e-9
    prob = df["Вероятность"].clip(0,100) / 100.0
    health = df["Здоровье"].clip(0,100) / 100.0
    opp = df["Сумма"].clip(lower=0)
    opp_norm = np.log1p(opp) / max(np.log1p(opp).max(), eps)

    smin, smax = float(df["Сортировка этапа"].min()), float(df["Сортировка этапа"].max())
    if smax - smin < eps:
        stage_closeness = pd.Series(0.5, index=df.index)
    else:
        stage_closeness = (df["Сортировка этапа"] - smin) / (smax - smin)
    stage_closeness = np.where(df["Этап ID"].astype(str).str.contains("LOSE", case=False, na=False), 0.0, stage_closeness)

    recency = 1 - (df["Дней без активности"].clip(lower=0) / max(horizon_days, 1)).clip(0,1)

    quick = 0.35*prob + 0.25*health + 0.15*recency + 0.15*stage_closeness + 0.10*opp_norm
    quick_score = (quick*100).round(1)

    eta = (30*(1-stage_closeness)*(1 - 0.5*health - 0.5*prob)).clip(lower=0).round(0)

    age_norm = (df["Дней в работе"]/max(df["Дней в работе"].max(),1)).clip(0,1)
    noact_norm = (df["Дней без активности"]/max(df["Дней без активности"].max(),1)).clip(0,1)
    drop = (1-prob)*0.4 + (1-health)*0.3 + noact_norm*0.2 + age_norm*0.1
    drop_score = (drop*100).round(1)

    out = df.copy()
    out["Скор быстрой победы"] = quick_score
    out["ETA дней"] = eta
    out["Скор отказа"] = drop_score
    out["Быстрая победа?"] = (out["Скор быстрой победы"]>=60) & (out["Вероятность"]>=min_prob) & (~out["Проиграна"])
    out["Стоп-лист?"] = (out["Скор отказа"]>=70) | (out["Проиграна"]) | ((out["Здоровье"]<40) & (out["Дней без активности"]>horizon_days))
    return out

def compute_conversion_by_manager_and_funnel(df, sort_map):
    results = []
    for (mgr, cat), g in df.groupby(["Менеджер","Воронка"], dropna=False):
        stages_sorted = sorted(g["Этап ID"].unique(), key=lambda s: sort_map.get(str(s), 9999))
        stage_counts = g.groupby("Этап ID").size()
        total = len(g)
        stage_data = []
        for s in stages_sorted:
            cnt = int(stage_counts.get(s, 0))
            conv = (cnt/total*100) if total>0 else 0
            stage_data.append({"Этап": s, "Количество": cnt, "Конверсия %": round(conv,1)})
        results.append({"Менеджер": mgr, "Воронка": cat, "Всего сделок": total, "Этапы": stage_data})
    return pd.DataFrame(results)

# =========================
# Сайдбар: фильтры
# =========================
st.sidebar.title("Фильтры")
date_from  = st.sidebar.date_input("С какой даты", datetime.now().date() - timedelta(days=30))
date_to    = st.sidebar.date_input("По какую дату", datetime.now().date())
stuck_days = st.sidebar.slider("Нет активности ≥ (дней)", 2, 21, 5)
limit      = st.sidebar.slider("Лимит сделок (API)", 50, 3000, 600, step=50)

st.sidebar.title("Настройки фокуса РОПа")
focus_horizon   = st.sidebar.slider("Горизонт фокуса (дней)", 7, 45, 14)
focus_min_prob  = st.sidebar.slider("Мин. вероятность для фокуса, %", 0, 100, 50)

# =========================
# Загрузка данных
# =========================
with st.spinner("Загружаю данные…"):
    if not BITRIX24_WEBHOOK:
        st.error("Не указан BITRIX24_WEBHOOK в Secrets. Укажите его, чтобы тянуть сделки из CRM.")
        st.stop()

    deals_raw = bx_get_deals(str(date_from), str(date_to), limit=limit)
    if not deals_raw:
        st.error("Сделок не найдено за выбранный период.")
        st.stop()
    df_raw = pd.DataFrame(deals_raw)
    df_raw["OPPORTUNITY"] = pd.to_numeric(df_raw.get("OPPORTUNITY"), errors="coerce").fillna(0.0)

    users_full = bx_get_users_full()
    users_map = {uid: users_full[uid]["name"] for uid in users_full}
    categories_map = bx_get_categories()
    open_tasks_map = bx_get_open_activities_for_deal_ids(df_raw["ID"].tolist())

# Основной скоринг
df_scores = compute_health_scores(df_raw, open_tasks_map, stuck_days=stuck_days)

# Адаптивный порядок и имена этапов
stage_ids = df_scores["Этап ID"].dropna().unique().tolist()
sort_map, name_map = bx_get_stage_map(stage_ids)

FALLBACK_ORDER = ["NEW","NEW_LEAD","PREPARATION","PREPAYMENT_INVOICE","EXECUTING","FINAL_INVOICE","WON","LOSE","LOSE_REASON"]
def fallback_sort(sid):
    sid = str(sid or "")
    sid_short = sid.split(":")[1] if ":" in sid else sid
    return (FALLBACK_ORDER.index(sid_short)*100 if sid_short in FALLBACK_ORDER else 10000 + hash(sid_short)%1000)

df_scores["Сортировка этапа"] = df_scores["Этап ID"].map(lambda s: sort_map.get(str(s), fallback_sort(s)))
df_scores["Название этапа"]   = df_scores["Этап ID"].map(lambda s: name_map.get(str(s), str(s)))
df_scores["Менеджер"]         = df_scores["Менеджер ID"].map(users_map).fillna("Неизвестно")
df_scores["Воронка"]          = df_scores["Воронка ID"].map(lambda x: categories_map.get(int(x or 0), "Основная"))

# Фильтр «только отдел продаж»
departments = bx_get_departments()
sales_depts = [d for d in departments if "продаж" in (d.get("NAME","").lower())]
sales_dept_ids = {int(d["ID"]) for d in sales_depts}
default_sales_only = bool(sales_dept_ids)

if departments:
    show_sales_only = st.sidebar.checkbox("Только сотрудники отдела продаж", value=default_sales_only)
    selected_depts = st.sidebar.multiselect(
        "Отделы (фильтр по сотрудникам)",
        options=[(int(d["ID"]), d["NAME"]) for d in departments],
        default=[(int(d["ID"]), d["NAME"]) for d in sales_depts],
        format_func=lambda t: t[1] if isinstance(t, tuple) else str(t)
    )
    selected_dept_ids = {t[0] for t in selected_depts} if selected_depts else sales_dept_ids
    if show_sales_only and selected_dept_ids:
        keep_ids = [uid for uid, info in users_full.items() if set(info["depts"]) & selected_dept_ids]
        df_scores = df_scores[df_scores["Менеджер ID"].isin(keep_ids)]

# Подсказчик РОПу
df_scores = focus_scores(df_scores, horizon_days=focus_horizon, min_prob=focus_min_prob)

# Глобальные фильтры по менеджерам/воронкам
funnels  = sorted(df_scores["Воронка"].unique())
managers = sorted(df_scores["Менеджер"].unique())
selected_funnels  = st.sidebar.multiselect("Воронки", funnels, default=funnels)
selected_managers = st.sidebar.multiselect("Менеджеры", managers, default=managers)

view_df = df_scores[(df_scores["Воронка"].isin(selected_funnels)) & (df_scores["Менеджер"].isin(selected_managers))].copy()
if view_df.empty:
    st.warning("Нет данных по выбранным фильтрам.")
    st.stop()

# =========================
# Шапка
# =========================
st.markdown("<div class='headerbar'><div class='pill'>БУРМАШ · Контроль отдела продаж</div></div>", unsafe_allow_html=True)
st.caption("Пульс воронки · Аудит · Менеджеры · Карточки · Отчёт · Фокус руководителю · План/факт")

c1,c2,c3,c4,c5 = st.columns(5, gap="small")
with c1: st.metric("Сделок", int(view_df.shape[0]))
with c2: st.metric("Объём, ₽", f"{int(view_df['Сумма'].sum()):,}".replace(","," "))
with c3: st.metric("Средний чек, ₽", f"{int(view_df['Сумма'].replace(0,np.nan).mean() or 0):,}".replace(","," "))
with c4: st.metric("Ср. здоровье", f"{view_df['Здоровье'].mean():.0f}%")
with c5: st.metric("Суммарный потенциал", int(view_df["Потенциал"].sum()))

# =========================
# Вкладки
# =========================
tab_focus, tab_pulse, tab_audit, tab_managers, tab_cards, tab_deal, tab_plan, tab_ai = st.tabs([
    "🎯 Фокус руководителю", "⛵ Пульс воронки", "🚧 Аудит", "👥 Менеджеры", "🗂 Карточки", "📄 Отчёт по сделке", "📅 План/факт", "🤖 AI-аналитика"
])

# --- ФОКУС РОПу ---
with tab_focus:
    st.markdown("#### Быстрые деньги (Quick Wins)")
    quick = (view_df[view_df["Быстрая победа?"]]
             .sort_values(["Скор быстрой победы","Сумма"], ascending=[False, False])
             .loc[:, ["ID сделки","Название","Менеджер","Название этапа","Сумма","Вероятность","Здоровье","Дней без активности","ETA дней","Скор быстрой победы"]]
             .head(30))
    if quick.empty:
        st.info("Пока нет сделок, удовлетворяющих условиям фокуса. Измените пороги в сайдбаре.")
    else:
        st.dataframe(quick.rename(columns={
            "ID сделки":"ID","Название":"Сделка","Менеджер":"Ответственный","Название этапа":"Этап",
            "Сумма":"Сумма, ₽","Вероятность":"Вероятность, %","Здоровье":"Здоровье, %",
            "Дней без активности":"Без активности, дн","ETA дней":"ETA, дн","Скор быстрой победы":"Quick Win, баллы"
        }), height=420, use_container_width=True)

    st.markdown("#### Стоит приостановить (Stop List)")
    stop = (view_df[view_df["Стоп-лист?"]]
            .sort_values(["Скор отказа","Дней без активности","Дней в работе"], ascending=[False, False, False])
            .loc[:, ["ID сделки","Название","Менеджер","Название этапа","Сумма","Вероятность","Здоровье","Дней в работе","Дней без активности","Скор отказа"]]
            .head(30))
    if stop.empty:
        st.success("Явных кандидатов на приостановку не найдено.")
    else:
        st.dataframe(stop.rename(columns={
            "ID сделки":"ID","Название":"Сделка","Менеджер":"Ответственный","Название этапа":"Этап",
            "Сумма":"Сумма, ₽","Вероятность":"Вероятность, %","Здоровье":"Здоровье, %",
            "Дней в работе":"Дней в работе","Дней без активности":"Без активности, дн","Скор отказа":"StopScore, баллы"
        }), height=380, use_container_width=True)

    # Быстрые действия
    st.markdown("#### Что сделать прямо сейчас")
    top_actions = view_df.sort_values("Скор быстрой победы", ascending=False).head(6)
    cols = st.columns(3, gap="medium")
    for i, (_, r) in enumerate(top_actions.iterrows()):
        with cols[i % 3]:
            st.markdown(f"""
            <div class="card">
              <div class="title">{r['Название']}</div>
              <div class="subtle">Этап: {r['Название этапа']} • Отв.: {r['Менеджер']}</div><hr/>
              <span class="badge good">Quick Win: <b>{r['Скор быстрой победы']}</b></span>
              <span class="badge">ETA: <b>{int(r['ETA дней'])} дн</b></span>
              <span class="badge">Вероятн.: <b>{int(r['Вероятность'])}%</b></span>
              <span class="badge">Сумма: <b>{int(r['Сумма']):,} ₽</b></span>
              <hr/>
              <div class="subtle">👉 {('Финальный созвон и счёт' if r['Вероятность']>=70 and r['Здоровье']>=70 else 'Назначить демо/встречу с ЛПР и выслать КП')}</div>
            </div>
            """, unsafe_allow_html=True)

# --- ПУЛЬС ВОРОНКИ ---
with tab_pulse:
    st.markdown("##### Воронка этапов (адаптивный порядок)")
    if px is None:
        st.info("Plotly недоступен.")
    else:
        metric_kind = st.radio("Показатель", ["Количество", "Сумма, ₽"], horizontal=True, key="metric_kind_funnel")
        funnel_df = (
            view_df.groupby(["Этап ID","Название этапа","Сортировка этапа"])
            .agg(Количество=("ID сделки","count"), Сумма=("Сумма","sum"))
            .reset_index()
            .sort_values("Сортировка этапа")
        )
        if metric_kind == "Количество":
            fig = px.funnel(funnel_df, x="Количество", y="Название этапа", color_discrete_sequence=["#ff7a00"])
        else:
            fig = px.funnel(funnel_df, x="Сумма", y="Название этапа", color_discrete_sequence=["#ff7a00"])
        fig.update_layout(margin=dict(l=10,r=10,t=10,b=10), height=420)
        st.plotly_chart(fig, use_container_width=True, key="chart_funnel_main")

    st.markdown("##### Тренд новых сделок по датам создания")
    if px:
        trend = view_df.copy()
        trend["date"] = pd.to_datetime(trend["Дата создания"]).dt.date
        trend = trend.groupby("date").agg(Количество=("ID сделки","count"), Сумма=("Сумма","sum")).reset_index()
        c1, c2 = st.columns(2, gap="large")
        with c1:
            fig1 = px.line(trend, x="date", y="Количество", markers=True, color_discrete_sequence=["#ff7a00"])
            fig1.update_layout(margin=dict(l=10,r=10,t=10,b=10), height=280)
            st.plotly_chart(fig1, use_container_width=True, key="chart_trend_qty")
        with c2:
            fig2 = px.area(trend, x="date", y="Сумма", color_discrete_sequence=["#111111"])
            fig2.update_layout(margin=dict(l=10,r=10,t=10,b=10), height=280)
            st.plotly_chart(fig2, use_container_width=True, key="chart_trend_sum")

    st.markdown("##### Лента изменений (последние)")
    st.dataframe(
        view_df.sort_values("Дата изменения", ascending=False)[
            ["ID сделки","Название","Менеджер","Название этапа","Сумма","Здоровье","Потенциал","Дата изменения"]
        ].head(200),
        height=360, use_container_width=True
    )

# --- АУДИТ ---
with tab_audit:
    st.markdown("##### Проблемные зоны")
    kpis = {
        "Без задач": int(view_df["Нет задач"].sum()),
        "Без контактов": int(view_df["Нет контакта"].sum()),
        "Без компаний": int(view_df["Нет компании"].sum()),
        "Застряли": int(view_df["Застряла"].sum()),
        "Проигранные": int(view_df["Проиграна"].sum()),
    }
    a,b,c,d,e = st.columns(5)
    a.metric("Без задач", kpis["Без задач"])
    b.metric("Без контактов", kpis["Без контактов"])
    c.metric("Без компаний", kpis["Без компаний"])
    d.metric("Застряли", kpis["Застряли"])
    e.metric("Проигранные", kpis["Проигранные"])

    if px:
        audit_df = pd.DataFrame({"Проблема": list(kpis.keys()), "Количество": list(kpis.values())}).sort_values("Количество", ascending=False)
        fig_p = px.bar(audit_df, x="Количество", y="Проблема", orientation="h",
                       color="Количество", color_continuous_scale=["#ffe8d6","#ff7a00"])
        fig_p.update_layout(coloraxis_showscale=False, margin=dict(l=10,r=10,t=10,b=10), height=320)
        st.plotly_chart(fig_p, use_container_width=True, key="chart_audit_bar")

    st.markdown("##### Списки по категориям")
    cols = st.columns(5, gap="small")
    lists = [
        ("Без задач", view_df["Нет задач"]),
        ("Без контактов", view_df["Нет контакта"]),
        ("Без компаний", view_df["Нет компании"]),
        ("Застряли", view_df["Застряла"]),
        ("Проигранные", view_df["Проиграна"]),
    ]
    for (title, mask), holder in zip(lists, cols):
        with holder:
            st.markdown(f"<div class='card'><div class='title'>{title}</div>", unsafe_allow_html=True)
            st.dataframe(
                view_df[mask][["ID сделки","Название","Менеджер","Название этапа","Сумма","Здоровье","Дней без активности"]].head(80),
                height=260, use_container_width=True
            )
            st.markdown("</div>", unsafe_allow_html=True)

# --- МЕНЕДЖЕРЫ ---
with tab_managers:
    st.markdown("##### Квадрант: здоровье × без задач (размер — сумма)")
    if px:
        quad = view_df.groupby("Менеджер").agg(
            health_avg=("Здоровье","mean"),
            no_tasks=("Нет задач","sum"),
            opp_sum=("Сумма","sum"),
            deals=("ID сделки","count")
        ).reset_index()
        fig_q = px.scatter(quad, x="health_avg", y="no_tasks", size="opp_sum",
                           hover_data=["deals","Менеджер"], color="health_avg",
                           color_continuous_scale=["#ffe8d6","#ff7a00","#111111"])
        fig_q.update_layout(coloraxis_showscale=False, margin=dict(l=10,r=10,t=10,b=10), height=420)
        st.plotly_chart(fig_q, use_container_width=True, key="chart_mgr_quad")

    st.markdown("##### Рейтинг по среднему здоровью")
    if px:
        rating = view_df.groupby("Менеджер").agg(health_avg=("Здоровье","mean"), deals=("ID сделки","count")).reset_index()
        rating = rating.sort_values("health_avg", ascending=True)
        fig_r = px.bar(rating, x="health_avg", y="Менеджер", orientation="h", text="deals",
                       color="health_avg", color_continuous_scale=["#ffe8d6","#ff7a00"])
        fig_r.update_traces(texttemplate="сделок: %{text}", textposition="outside", cliponaxis=False)
        fig_r.update_layout(coloraxis_showscale=False, margin=dict(l=10,r=10,t=10,b=10), height=520)
        st.plotly_chart(fig_r, use_container_width=True, key="chart_mgr_rating")

    st.markdown("##### Конверсия по этапам (по менеджерам и воронкам)")
    conv_data = compute_conversion_by_manager_and_funnel(view_df, sort_map)
    for idx, row in conv_data.iterrows():
        with st.expander(f"👤 {row['Менеджер']} | {row['Воронка']} ({row['Всего сделок']} сделок)"):
            stage_df = pd.DataFrame(row['Этапы'])
            # заменить ID этапа на имя, если есть
            stage_df["Этап"] = stage_df["Этап"].map(lambda s: name_map.get(str(s), str(s)))
            st.dataframe(stage_df, use_container_width=True)
            if px and not stage_df.empty:
                fig_f = px.funnel(stage_df, x="Количество", y="Этап", title="Воронка конверсии")
                st.plotly_chart(fig_f, use_container_width=True, key=f"chart_conv_funnel_{idx}")

# --- КАРТОЧКИ ---
with tab_cards:
    st.markdown("##### Приоритетные сделки (сначала слабые по здоровью)")
    pick = view_df.sort_values(["Здоровье","Потенциал","Сумма"], ascending=[True,False,False]).head(30)
    cols = st.columns(3, gap="medium")
    for i, (_, row) in enumerate(pick.iterrows()):
        with cols[i % 3]:
            badge_cls = "bad" if row["Здоровье"] < 60 else ("good" if row["Здоровье"]>=80 else "warn")
            risks_list = [name for name,flag in {
                "без задач": row["Нет задач"], "без компании": row["Нет компании"],
                "без контактов": row["Нет контакта"], "застряла": row["Застряла"]
            }.items() if flag]
            steps = []
            if row["Нет задач"]: steps.append("Поставить задачу со сроком.")
            if row["Застряла"]:  steps.append("Связаться сегодня и обновить этап.")
            if row["Вероятность"]<40 and row["Сумма"]>0: steps.append("Уточнить бюджет/срок/ЛПР и обновить вероятность.")
            if not steps: steps.append("Зафиксировать следующий шаг и дату.")
            st.markdown(f"""
            <div class="card">
              <div class="title">{row['Название']}</div>
              <div class="subtle">ID {row['ID сделки']} • {row['Менеджер']}</div>
              <hr/>
              <span class="badge {badge_cls}">Здоровье: <b>{int(row['Здоровье'])}%</b></span>
              <span class="badge">Потенциал: <b>{int(row['Потенциал'])}%</b></span>
              <span class="badge">Сумма: <b>{int(row['Сумма']):,} ₽</b></span>
              <span class="badge">Этап: <b>{row['Название этапа']}</b></span>
              <span class="badge">Без активности: <b>{row['Дней без активности']} дн</b></span>
              <hr/>
              <div class="subtle">⚠️ Риски: {", ".join(risks_list) or "нет"}</div>
              <div class="subtle">✅ Следующие шаги:<br/>• {"<br/>• ".join(steps)}</div>
            </div>
            """, unsafe_allow_html=True)

# --- ОТЧЁТ ПО СДЕЛКЕ ---
with tab_deal:
    st.markdown("##### Подробный отчёт по сделке")
    options = view_df.sort_values("Дата изменения", ascending=False)
    if options.empty:
        st.info("Нет сделок по текущим фильтрам.")
        st.stop()
    label_map = {int(r.ID): f"[{int(r.ID)}] {r.Название} — {r.Менеджер}" for r in options[["ID сделки","Название","Менеджер"]].rename(columns={"ID сделки":"ID"}).itertuples(index=False)}
    chosen_id = st.selectbox("Сделка", list(label_map.keys()), format_func=lambda x: label_map[x])
    deal = view_df[view_df["ID сделки"]==chosen_id].iloc[0]

    a,b,c,d = st.columns(4)
    with a:
        st.markdown(f"<div class='title'>{deal['Название']}</div>", unsafe_allow_html=True)
        st.caption(f"Компания: БУРМАШ • Ответственный: {deal['Менеджер']} • Этап: {deal['Название этапа']}")
    with b: st.markdown(f"<div class='score'>{int(deal['Потенциал'])}</div><div class='kpi-caption'>Потенциал</div>", unsafe_allow_html=True)
    with c: st.markdown(f"<div class='score'>{int(deal['Здоровье'])}</div><div class='kpi-caption'>Здоровье</div>", unsafe_allow_html=True)
    with d: st.markdown(f"<div class='kpi'>{int(deal['Сумма'])}</div><div class='kpi-caption'>Сумма, ₽</div>", unsafe_allow_html=True)

    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("<div class='title'>Параметры</div>", unsafe_allow_html=True)
        st.markdown(f"""
        <div class="grid2">
          <div class="card"><div class="title">Сумма</div><div class="kpi">{int(deal['Сумма'])}</div><div class="kpi-caption">вероятность {int(deal['Вероятность'])}%</div></div>
          <div class="card"><div class="title">Сроки</div><div class="kpi">{int(deal['Дней в работе'])}</div><div class="kpi-caption">без активности {int(deal['Дней без активности'])} дн</div></div>
        </div>
        """, unsafe_allow_html=True)

        fin = ("Бюджет не подтверждён, сумма в сделке = 0." if deal["Сумма"]<=0
               else ("Бюджет обсуждается, вероятность низкая — требуется подтверждение ЛПР и КП."
                     if deal["Вероятность"]<40 else
                     "Бюджет ориентировочно подтверждён, требуется финализация условий."))
        lpr = "Контакт есть" if not deal["Нет контакта"] else "ЛПР не указан — подтвердите ФИО и роль."
        need = "Интерес подтверждён; уточните критерии успеха и сроки." if deal["Вероятность"]>=30 else "Потребность не зафиксирована — сформулируйте задачу и результат."
        timebox = ("Нет задачи на следующий шаг — согласуйте дату контакта." if deal["Нет задач"]
                   else ("Просрочка активности — сделайте контакт и обновите этап." if deal["Застряла"]
                         else "Сроки контролируются задачами."))
        main_task = "Назначить встречу/демо и прислать КП" if deal["Вероятность"]<50 else "Согласовать условия и направить договор/счёт"

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
        st.markdown("<div class='title'>Итоги работы</div>", unsafe_allow_html=True)
        risks_list = [name for name,flag in {
            "без задач": deal["Нет задач"], "без контактов": deal["Нет контакта"],
            "без компании": deal["Нет компании"], "застряла": deal["Застряла"]
        }.items() if flag]
        st.markdown(f"""
        <div class="card"><div class="title">Итог</div>
        <div class="subtle">Этап: {deal['Название этапа'] or '—'}. Последняя активность: {str(deal['Последняя активность'])[:19]}.<br/>
        Риски: {", ".join(risks_list) if risks_list else "существенных рисков не выявлено"}.</div></div>
        """, unsafe_allow_html=True)

# --- ПЛАН/ФАКТ ---
with tab_plan:
    st.subheader("Годовой план по выручке")
    yearly_target = st.number_input("Цель на год, ₽", min_value=0, value=10_000_000, step=100_000, format="%d")
    start_month = st.selectbox("Стартовый месяц отчёта", list(range(1,13)), index=datetime.now().month-1)

    df_year = view_df.copy()
    df_year["Дата создания"] = pd.to_datetime(df_year["Дата создания"])
    df_year = df_year[df_year["Дата создания"].dt.year == datetime.now().year]
    df_year["Месяц"] = df_year["Дата создания"].dt.month

    actual = df_year.groupby("Месяц")["Сумма"].sum().reindex(range(1,12+1), fill_value=0)
    months = list(range(start_month, start_month+12))
    months = [((m-1)%12)+1 for m in months]

    current_month = datetime.now().month
    months_left = [m for m in months if m >= current_month]
    revenue_to_go = yearly_target - actual.sum()
    monthly_plan = {m: max(0, revenue_to_go/len(months_left)) for m in months_left} if months_left else {}

    plan_df = pd.DataFrame({
        "Месяц": months,
        "Факт, ₽": [actual.get(m,0) for m in months],
        "План, ₽": [monthly_plan.get(m,0) if m in monthly_plan else None for m in months]
    }).copy()
    plan_df["План, ₽"] = plan_df["План, ₽"].ffill().bfill()

    if px:
        fig_plan = px.area(
            plan_df, x="Месяц", y=["Факт, ₽","План, ₽"],
            labels={"value":"Сумма, ₽","Месяц":"Месяц"},
            title="Факт vs План по месяцам",
            color_discrete_map={"Факт, ₽":"#111111","План, ₽":"#ff7a00"}
        )
        st.plotly_chart(fig_plan, use_container_width=True, key="chart_year_plan")

    st.dataframe(
        plan_df.assign(**{"Отклонение, ₽": plan_df["Факт, ₽"] - plan_df["План, ₽"]}).round(0),
        use_container_width=True
    )

# --- AI (опционально) ---
with tab_ai:
    st.subheader("🤖 AI-аналитика по менеджерам")
    if not PERPLEXITY_API_KEY:
        st.info("PERPLEXITY_API_KEY не задан — AI-аналитика отключена.")
    else:
        for mgr in selected_managers:
            mg = view_df[view_df["Менеджер"]==mgr]
            if mg.empty: continue
            summary = {
                "total_deals": int(len(mg)),
                "revenue": int(mg["Сумма"].sum()),
                "avg_health": float(mg["Здоровье"].mean()),
                "no_tasks": int(mg["Нет задач"].sum()),
                "no_company": int(mg["Нет компании"].sum()),
                "no_contact": int(mg["Нет контакта"].sum()),
                "stuck": int(mg["Застряла"].sum()),
                "lost": int(mg["Проиграна"].sum()),
                "won": int(len(mg[mg["Этап ID"].astype(str).str.contains("WON", case=False)]))
            }
            with st.expander(f"👤 {mgr} ({summary['total_deals']} сделок)"):
                # лёгкая локальная «AI-заглушка», если сеть не доступна
                try:
                    import json
                    data = {
                        "model": "sonar-pro",
                        "messages": [
                            {"role":"system","content":"Ты эксперт по CRM-аналитике."},
                            {"role":"user","content": f"Проанализируй KPI менеджера: {mgr}. Данные: {summary}. Дай 3 блока: сильные стороны, проблемы, рекомендации."}
                        ],
                        "max_tokens": 600, "temperature": 0.3
                    }
                    resp = requests.post(
                        "https://api.perplexity.ai/chat/completions",
                        headers={"Authorization": f"Bearer {PERPLEXITY_API_KEY}"},
                        json=data, timeout=30
                    )
                    txt = resp.json()["choices"][0]["message"]["content"]
                except Exception as e:
                    txt = f"_AI недоступен: {e}_"
                st.markdown(txt)

st.markdown("---")
st.caption("БУРМАШ · CRM Дэшборд v4.3")
