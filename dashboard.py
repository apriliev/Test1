# -*- coding: utf-8 -*-
"""
RUBI-like CRM Dashboard (Streamlit, v2 — менеджер-фильтр + "Отчёт как в РУБИ" + рекомендации)
- Глобальный фильтр по менеджерам (распространяется на все вкладки)
- "Отчёт по сделке" в стиле РУБИ ЧАТ: карточки, индикаторы, мини-график динамики, оценка коммуникации
- Персональные рекомендации Next Best Actions по каждой сделке (правила без внешнего ИИ)
- Источник данных: Bitrix24 (webhook) или офлайн (CSV/XLSX)
- Экспорт: ZIP с CSV (без Excel-зависимостей)
"""

import os
import json
import time
import hashlib
from datetime import datetime, timedelta
from io import BytesIO
import zipfile
import math
import random

import numpy as np
import pandas as pd
import streamlit as st

# Графики (опционально, если plotly нет — UI работает без них)
try:
    import plotly.express as px
except Exception:
    px = None

# =========================
# БАЗОВЫЕ НАСТРОЙКИ UI
# =========================
st.set_page_config(page_title="RUBI-like CRM Аналитика", page_icon="📈", layout="wide")

CUSTOM_CSS = """
<style>
:root { --rubi-accent:#6C5CE7; --rubi-red:#ff4d4f; --rubi-green:#22c55e; --rubi-yellow:#f59e0b; }
.block-container { padding-top: 0.8rem; padding-bottom: 1.2rem; }
.rubi-card { border-radius:18px; padding:18px 18px 12px; background:#111418; border:1px solid #222; box-shadow:0 4px 18px rgba(0,0,0,.25); }
.rubi-title { font-weight:700; font-size:18px; margin-bottom:6px; }
.rubi-chip { display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:999px; border:1px solid #2a2f36; background:#0e1216; font-size:12px; margin-right:6px; margin-bottom:6px;}
.rubi-good { color: var(--rubi-green) !important; }
.rubi-bad  { color: var(--rubi-red) !important; }
.rubi-warn { color: var(--rubi-yellow) !important; }
.small { opacity:.8; font-size:12px; }
hr { border: 0; border-top:1px solid #222; margin: 10px 0 6px }
div[data-testid="stMetricValue"] { font-size:22px !important; }
.kpi-number { font-weight:800; font-size:28px; }
.kpi-caption { color:#a8b3bf; font-size:12px; margin-top:-6px }
.score-circle { width:64px;height:64px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;background:#0f141a;border:1px solid #2b3139;font-weight:800;font-size:22px;margin-right:10px}
.grid-3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
.grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# =========================
# ПРОСТАЯ АВТОРИЗАЦИЯ
# логин: admin / пароль: 123  (можешь заменить хэш ниже)
# =========================
def check_password():
    def password_entered():
        ok_user = st.session_state.get("username") in {"admin"}
        # sha256("123")
        target_hash = "a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3"
        ok_pass = hashlib.sha256(st.session_state.get("password","").encode()).hexdigest() == target_hash
        st.session_state["password_correct"] = bool(ok_user and ok_pass)
        st.session_state.pop("password", None)

    if st.secrets.get("DISABLE_AUTH", False):
        st.session_state["password_correct"] = True

    if "password_correct" not in st.session_state or not st.session_state["password_correct"]:
        st.markdown("### 🔐 Вход в систему")
        st.text_input("Логин", key="username")
        st.text_input("Пароль", type="password", key="password", on_change=password_entered)
        st.stop()

check_password()
with st.sidebar:
    if st.button("Выйти"):
        st.session_state["password_correct"] = False
        st.rerun()

# =========================
# СЕКРЕТЫ / ПЕРЕМЕННЫЕ
# =========================
def get_secret(name, default=None):
    if name in st.secrets:
        return st.secrets[name]
    return os.getenv(name, default)

BITRIX24_WEBHOOK = (get_secret("BITRIX24_WEBHOOK", "") or "").strip()
PERPLEXITY_API_KEY = (get_secret("PERPLEXITY_API_KEY", "") or "").strip()
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"

# =========================
# BITRIX24 HELPERS (опционально)
# =========================
def _bx_get(method, params=None, pause=0.4):
    """Безопасный GET к Bitrix24 с авто-пагинацией."""
    url = BITRIX24_WEBHOOK.rstrip("/") + f"/{method}.json"
    out, start = [], 0
    params = dict(params or {})
    while True:
        params["start"] = start
        import requests  # локальный импорт, чтобы офлайн-режим не требовал requests
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        res = data.get("result")
        if isinstance(res, dict) and "items" in res:
            batch = res.get("items", [])
        else:
            batch = res or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 50:
            break
        start += 50
        time.sleep(pause)
    return out

@st.cache_data(show_spinner=False, ttl=300)
def bx_get_deals(date_from=None, date_to=None, limit=1000):
    params = {"select[]":[
        "ID","TITLE","STAGE_ID","OPPORTUNITY","ASSIGNED_BY_ID",
        "COMPANY_ID","CONTACT_ID","PROBABILITY",
        "DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME"
    ]}
    if date_from: params["filter[>=DATE_CREATE]"] = date_from
    if date_to:   params["filter[<=DATE_CREATE]"] = date_to
    deals = _bx_get("crm.deal.list", params)
    return deals[:limit]

@st.cache_data(show_spinner=False, ttl=300)
def bx_get_users():
    users = _bx_get("user.get", {})
    return {int(u["ID"]): (u.get("NAME","")+ " " + u.get("LAST_NAME","")).strip() or u.get("LOGIN", "") for u in users}

@st.cache_data(show_spinner=False, ttl=300)
def bx_get_open_activities_for_deal_ids(deal_ids):
    """Открытые активити по сделкам (есть задачи → не 'без задач')."""
    out = {}
    if not deal_ids:
        return out
    for chunk in np.array_split(list(map(int, deal_ids)), max(1, len(deal_ids)//40 + 1)):
        params = {
            "filter[OWNER_TYPE_ID]": 2,  # 2 = Deal
            "filter[OWNER_ID]": ",".join(map(str, chunk)),
            "filter[COMPLETED]": "N"
        }
        acts = _bx_get("crm.activity.list", params)
        for a in acts:
            k = int(a["OWNER_ID"])
            out.setdefault(k, []).append(a)
    return out

# =========================
# ОБЩИЕ ФУНКЦИИ
# =========================
def to_dt(x):
    try:
        return pd.to_datetime(x)
    except Exception:
        return pd.NaT

def compute_health_scores(df, open_tasks_map, stuck_days=5):
    """Считает здоровье/потенциал/флаги на каждую сделку."""
    now = pd.Timestamp.utcnow()
    rows = []
    for _, r in df.iterrows():
        last = to_dt(r.get("LAST_ACTIVITY_TIME")) or to_dt(r.get("DATE_MODIFY")) or to_dt(r.get("DATE_CREATE"))
        days_in_work = max(0, (now - to_dt(r.get("DATE_CREATE"))).days if pd.notna(to_dt(r.get("DATE_CREATE"))) else 0)
        days_no_activity = (now - (last if pd.notna(last) else now)).days
        has_task = len(open_tasks_map.get(int(r["ID"]), [])) > 0

        flags = {
            "no_company": int(r.get("COMPANY_ID") or 0) == 0,
            "no_contact": int(r.get("CONTACT_ID") or 0) == 0,
            "no_tasks": not has_task,
            "stuck": days_no_activity >= stuck_days,
            "lost": str(r.get("STAGE_ID","")).upper().find("LOSE") >= 0
        }

        score = 100
        if flags["no_company"]: score -= 10
        if flags["no_contact"]: score -= 10
        if flags["no_tasks"]:   score -= 25
        if flags["stuck"]:      score -= 25
        if flags["lost"]:       score = min(score, 15)

        opp = float(r.get("OPPORTUNITY") or 0.0)
        prob = float(r.get("PROBABILITY") or 0.0)
        potential = min(100, int((opp > 0) * (30 + min(70, math.log10(max(1, opp))/5 * 70)) * (0.4 + prob/100*0.6)))

        rows.append({
            "ID": int(r["ID"]),
            "TITLE": r.get("TITLE",""),
            "ASSIGNED_BY_ID": int(r.get("ASSIGNED_BY_ID") or 0),
            "STAGE_ID": r.get("STAGE_ID",""),
            "OPPORTUNITY": opp,
            "PROBABILITY": prob,
            "DATE_CREATE": to_dt(r.get("DATE_CREATE")),
            "DATE_MODIFY": to_dt(r.get("DATE_MODIFY")),
            "LAST_ACTIVITY_TIME": last,
            "days_in_work": days_in_work,
            "days_no_activity": days_no_activity,
            "health": max(0, min(100, int(score))),
            "potential": max(0, min(100, int(potential))),
            "flag_no_company": flags["no_company"],
            "flag_no_contact": flags["no_contact"],
            "flag_no_tasks": flags["no_tasks"],
            "flag_stuck": flags["stuck"],
            "flag_lost": flags["lost"],
        })
    return pd.DataFrame(rows)

def split_green_red(df_scores):
    grp = df_scores.groupby("ASSIGNED_BY_ID").agg(
        deals=("ID","count"),
        health_avg=("health","mean"),
        potential_sum=("potential","sum"),
        opp_sum=("OPPORTUNITY","sum"),
        no_tasks=("flag_no_tasks","sum"),
        stuck=("flag_stuck","sum"),
        lost=("flag_lost","sum"),
    ).reset_index()
    grp["zone"] = np.where((grp["health_avg"]>=70) & (grp["no_tasks"]<=2) & (grp["stuck"]<=2), "green", "red")
    return grp

# ---------- РЕКОМЕНДАЦИИ (rule-based) ----------
def deal_recommendations(row):
    recs = []
    # приоритизация
    if row["flag_lost"]:
        recs.append("Проверьте корректность причины проигрыша и верните сделку в работу, если есть шанс — предложите альтернативу/рассрочку.")
        return recs
    if row["flag_no_tasks"]:
        recs.append("Поставьте задачу на следующий шаг с датой и комментарием. Нельзя оставлять сделку без активной задачи.")
    if row["flag_stuck"]:
        recs.append("Нет активности более нескольких дней — свяжитесь с клиентом сегодня: звонок + письмо с резюме договорённостей.")
    if row["flag_no_contact"]:
        recs.append("Добавьте контакт ЛПР (ФИО, телефон/email). Без ЛПР шанс закрытия низкий.")
    if row["flag_no_company"]:
        recs.append("Заполните карточку компании: ИНН, сайт, отрасль — это нужно для отчётов и сопоставления.")
    # потенциал/здоровье
    if row["health"] < 60 and row["potential"] >= 50:
        recs.append("Низкое здоровье при высоком потенциале — согласуйте встречу/демо, ускорьте согласование ТЗ и КП.")
    if row["OPPORTUNITY"] > 0 and row["PROBABILITY"] < 40:
        recs.append("Сумма заявлена, но вероятность низкая — уточните бюджет/сроки/ЛПР, обновите вероятность.")
    if row["days_in_work"] > 20 and row["PROBABILITY"] < 30:
        recs.append("Долгое ведение без прогресса — либо поднимите этап и договорённости, либо жёстко переформатируйте план действий.")
    if not recs:
        recs.append("Продолжайте план по этапу: подтвердите следующую встречу, зафиксируйте договорённости в комментарии.")
    return recs

# ---- Оценка коммуникации (5 аспектов, 0-100) ----
def comm_scores(row):
    # Простая эвристика
    contact = 30 + (0 if row["flag_no_contact"] else 30) + (0 if row["flag_no_company"] else 10) + (10 if not row["flag_stuck"] else 0)
    need = 30 + (20 if not row["flag_no_tasks"] else 0) + (10 if row["OPPORTUNITY"]>0 else 0) + (0 if row["flag_stuck"] else 10)
    present = 20 + int(row["PROBABILITY"]/2) + (10 if row["potential"]>50 else 0)
    struct = 30 + (20 if not row["flag_no_tasks"] else 0) + (10 if row["days_no_activity"]<=2 else 0)
    close = 20 + int(row["PROBABILITY"]) + (10 if row["OPPORTUNITY"]>0 else 0) - (10 if row["flag_stuck"] else 0)
    s = { 
        "Открытие контакта": int(np.clip(contact, 0, 100)),
        "Выявление потребности": int(np.clip(need, 0, 100)),
        "Презентация и аргументация": int(np.clip(present, 0, 100)),
        "Ведение диалога": int(np.clip(struct, 0, 100)),
        "Закрытие и фиксация": int(np.clip(close, 0, 100)),
    }
    return s

# ---- Генерация "контента" для карточек отчёта ----
def report_texts(row):
    # Фин. готовность
    if row["OPPORTUNITY"] <= 0:
        fin = "Бюджет не подтверждён, сумма в сделке = 0."
    elif row["PROBABILITY"] < 40:
        fin = "Бюджет обсуждается, вероятность низкая — требуется подтверждение ЛПР и КП."
    else:
        fin = "Бюджет ориентировочно подтверждён, требуется финализация условий."
    # ЛПР
    lpr = "Контакт есть" if not row["flag_no_contact"] else "ЛПР не указан — подтвердите ФИО и роль."
    # Потребность
    need = "Интерес подтверждён на текущем этапе, уточните критерии успеха и сроки." if row["PROBABILITY"]>=30 else "Потребность не зафиксирована — сформулируйте задачу клиента и желаемый результат."
    # Сроки
    if row["flag_no_tasks"]:
        timebox = "Нет задачи на следующий шаг — согласуйте дату контакта."
    elif row["flag_stuck"]:
        timebox = "Просрочка по активности — требуется быстрый контакт и обновление этапа."
    else:
        timebox = "Сроки контролируются задачами."
    # Задача карточки
    main_task = "Назначить встречу/демо и прислать КП" if row["PROBABILITY"]<50 else "Согласовать условия и направить договор/счёт"
    return fin, lpr, need, timebox, main_task

# ---- Таймлайн активности (игрушечная серия) ----
def activity_series(row, points=60):
    start = row["DATE_CREATE"] if pd.notna(row["DATE_CREATE"]) else pd.Timestamp.utcnow() - pd.Timedelta(days=30)
    end = pd.Timestamp.utcnow()
    if start > end: start = end - pd.Timedelta(days=1)
    idx = pd.date_range(start, end, periods=points)
    y = np.random.default_rng(int(row["ID"])).normal(0.1, 0.02, size=points).clip(0,1)
    # буст около создания и последней активности
    near_start = np.argmin(np.abs(idx - start))
    near_last = np.argmin(np.abs(idx - (row["LAST_ACTIVITY_TIME"] if pd.notna(row["LAST_ACTIVITY_TIME"]) else end)))
    for i in range(points):
        y[i] += 0.4 * math.exp(-abs(i-near_start)/6)
        y[i] += 0.8 * math.exp(-abs(i-near_last)/4)
    return pd.DataFrame({"ts": idx, "activity": y})

# =========================
# БОКОВАЯ ПАНЕЛЬ / ФИЛЬТРЫ
# =========================
st.sidebar.title("Фильтры")
company_alias = st.sidebar.text_input("Компания (в шапке)", "ООО «Фокус»")
date_from = st.sidebar.date_input("С какой даты", datetime.now().date() - timedelta(days=30))
date_to   = st.sidebar.date_input("По какую дату", datetime.now().date())
stuck_days = st.sidebar.slider("Нет активности ≥ (дней)", 2, 21, 5)
limit = st.sidebar.slider("Лимит сделок (API)", 50, 3000, 600, step=50)

# Офлайн-файл (если нет вебхука)
uploaded_offline = None
if not BITRIX24_WEBHOOK:
    st.sidebar.warning("BITRIX24_WEBHOOK не задан — офлайн-режим (загрузите CSV/XLSX).")
    uploaded_offline = st.sidebar.file_uploader("CSV/XLSX со сделками", type=["csv","xlsx"])

# =========================
# ЗАГРУЗКА ДАННЫХ
# =========================
with st.spinner("Готовлю данные…"):
    if BITRIX24_WEBHOOK:
        deals_raw = bx_get_deals(str(date_from), str(date_to), limit=limit)
        if not deals_raw:
            st.error("За выбранный период сделок не найдено (Bitrix24).")
            st.stop()
        df_raw = pd.DataFrame(deals_raw)
        df_raw["OPPORTUNITY"] = pd.to_numeric(df_raw.get("OPPORTUNITY"), errors="coerce").fillna(0.0)
        users_map = bx_get_users()
        open_tasks_map = bx_get_open_activities_for_deal_ids(df_raw["ID"].tolist())
    else:
        if not uploaded_offline:
            st.info("Загрузите CSV/XLSX с колонками минимум: ID, TITLE, STAGE_ID, OPPORTUNITY, ASSIGNED_BY_ID, "
                    "COMPANY_ID, CONTACT_ID, PROBABILITY, DATE_CREATE, DATE_MODIFY, LAST_ACTIVITY_TIME.")
            st.stop()
        # читаем офлайн-таблицу
        if uploaded_offline.name.lower().endswith(".csv"):
            df_raw = pd.read_csv(uploaded_offline)
        else:
            df_raw = pd.read_excel(uploaded_offline)

        df_raw.columns = [c.strip() for c in df_raw.columns]
        must = ["ID","TITLE","STAGE_ID","OPPORTUNITY","ASSIGNED_BY_ID",
                "COMPANY_ID","CONTACT_ID","PROBABILITY","DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME"]
        missing = [c for c in must if c not in df_raw.columns]
        if missing:
            st.error(f"Не хватает колонок: {missing}")
            st.stop()
        df_raw["OPPORTUNITY"] = pd.to_numeric(df_raw["OPPORTUNITY"], errors="coerce").fillna(0.0)
        users_map = {int(i): str(i) for i in pd.to_numeric(df_raw["ASSIGNED_BY_ID"], errors="coerce").fillna(0).astype(int).unique()}
        if "manager" in df_raw.columns:
            for aid, name in df_raw[["ASSIGNED_BY_ID","manager"]].dropna().values:
                try:
                    users_map[int(aid)] = str(name)
                except Exception:
                    pass
        open_tasks_map = {}  # в офлайне считаем, что задач нет (или добавь колонку для явного признака)

    # Расчёты
    df_scores = compute_health_scores(df_raw, open_tasks_map, stuck_days=stuck_days)
    df_scores["manager"] = df_scores["ASSIGNED_BY_ID"].map(users_map).fillna("Неизвестно")

# ---------- Глобальный фильтр по менеджерам ----------
manager_options = sorted(df_scores["manager"].unique())
selected_managers = st.sidebar.multiselect("Фильтр по менеджерам", manager_options, default=[])
view_df = df_scores[df_scores["manager"].isin(selected_managers)] if selected_managers else df_scores.copy()
mgr = split_green_red(view_df)

# =========================
# ВЕРХ ШАПКИ
# =========================
st.title("БУРМАШ Контроль отдела продаж")
st.caption("Автоаудит • Пульс • Зоны менеджеров • Карточки • Отчёт по сделке • Экспорт CSV")

# Топ-метрики (по view)
c1,c2,c3,c4,c5 = st.columns(5, gap="small")
with c1: st.metric("Всего сделок", int(view_df.shape[0]))
with c2: st.metric("Объём, ₽", f"{int(view_df['OPPORTUNITY'].sum()):,}".replace(","," "))
with c3: st.metric("Средний чек, ₽", f"{int(view_df['OPPORTUNITY'].replace(0,np.nan).mean() or 0):,}".replace(","," "))
with c4: st.metric("Средн. здоровье", f"{view_df['health'].mean():.0f}%")
with c5: st.metric("Суммарный потенциал", int(view_df["potential"].sum()))

# =========================
# ВКЛАДКИ
# =========================
tab_pulse, tab_audit, tab_managers, tab_cards, tab_deal, tab_export = st.tabs([
    "⛵ Пульс сделок", "🚁 Аудит воронки", "🚀 Менеджеры", "🗂 Карточки", "📄 Отчёт по сделке", "⬇️ Экспорт (CSV)"
])

# --- ПУЛЬС
with tab_pulse:
    if px is None:
        st.warning("Plotly недоступен — графики отключены.")
    else:
        a,b = st.columns([3,2], gap="large")
        with a:
            st.subheader("Динамика по этапам")
            stage_df = view_df.groupby("STAGE_ID").agg(Сумма=("OPPORTUNITY","sum"), Количество=("ID","count")).reset_index()
            fig = px.bar(stage_df, x="STAGE_ID", y="Сумма", text="Количество")
            st.plotly_chart(fig, use_container_width=True)
        with b:
            st.subheader("Распределение здоровья")
            fig2 = px.histogram(view_df, x="health", nbins=20)
            st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Лента изменений (последние)")
    st.dataframe(
        view_df.sort_values("DATE_MODIFY", ascending=False)[
            ["ID","TITLE","manager","STAGE_ID","OPPORTUNITY","health","potential","DATE_MODIFY"]
        ].head(200),
        height=360
    )

# --- АУДИТ
with tab_audit:
    st.subheader("Проблемные зоны (по фильтру)")
    kpis = {
        "Сделок без задач": int((~view_df["ID"].isin(open_tasks_map.keys())).sum()),
        "Сделок без контактов": int(view_df["flag_no_contact"].sum()),
        "Сделок без компаний": int(view_df["flag_no_company"].sum()),
        "Застрявшие сделки": int(view_df["flag_stuck"].sum()),
        "Потерянные сделки": int(view_df["flag_lost"].sum()),
    }
    a,b,c,d,e = st.columns(5)
    a.metric("Без задач", kpis["Сделок без задач"])
    b.metric("Без контактов", kpis["Сделок без контактов"])
    c.metric("Без компаний", kpis["Сделок без компаний"])
    d.metric("Застряли", kpis["Застрявшие сделки"])
    e.metric("Потерянные", kpis["Потерянные сделки"])

    st.markdown("##### Списки по категориям")
    cols = st.columns(5, gap="small")
    lists = [
        ("Без задач", ~view_df["ID"].isin(open_tasks_map.keys())),
        ("Без контактов", view_df["flag_no_contact"]),
        ("Без компаний", view_df["flag_no_company"]),
        ("Застряли", view_df["flag_stuck"]),
        ("Потерянные", view_df["flag_lost"]),
    ]
    for (title, mask), holder in zip(lists, cols):
        with holder:
            st.markdown(f'<div class="rubi-card"><div class="rubi-title">{title}</div>', unsafe_allow_html=True)
            st.dataframe(
                view_df[mask][["ID","TITLE","manager","STAGE_ID","OPPORTUNITY","health","days_no_activity"]].head(80),
                height=260
            )
            st.markdown("</div>", unsafe_allow_html=True)

# --- МЕНЕДЖЕРЫ
with tab_managers:
    st.subheader("Зелёная / Красная зоны по менеджерам (по фильтру)")
    mgr = split_green_red(view_df)
    mgr["manager"] = mgr["ASSIGNED_BY_ID"].map(users_map).fillna("Неизвестно")

    left, right = st.columns([1.5,1], gap="large")

    with left:
        if px is None:
            st.info("Plotly недоступен — диаграмма отключена.")
        else:
            fig = px.scatter(
                mgr, x="health_avg", y="no_tasks", size="opp_sum", color="zone",
                hover_data=["manager","deals","stuck","lost","potential_sum"],
                labels={"health_avg":"Средн. здоровье","no_tasks":"Без задач (шт)"}
            )
            st.plotly_chart(fig, use_container_width=True)

        st.markdown("**Таблица менеджеров**")
        st.dataframe(
            mgr[["manager","deals","opp_sum","health_avg","no_tasks","stuck","lost","zone"]]
            .sort_values(["zone","health_avg"], ascending=[True,False]),
            height=380
        )

    with right:
        st.markdown("#### Лидеры и рисковые")
        agg = view_df.groupby("manager").agg(
            deals=("ID","count"),
            health_avg=("health","mean"),
            opp=("OPPORTUNITY","sum"),
            stuck=("flag_stuck","sum"),
            no_tasks=("flag_no_tasks","sum"),
            lost=("flag_lost","sum"),
        ).reset_index()

        st.markdown("**Зелёная зона**")
        st.dataframe(agg.query("health_avg>=70").sort_values("health_avg", ascending=False).head(10), height=180)
        st.markdown("**Красная зона**")
        st.dataframe(agg.query("health_avg<70 or no_tasks>2 or stuck>2")
                     .sort_values(["health_avg","no_tasks","stuck"], ascending=[True,False,False]).head(10), height=180)

# --- КАРТОЧКИ
with tab_cards:
    st.subheader("Карточки сделок с быстрым планом")
    pick = view_df.sort_values(["health","potential","OPPORTUNITY"], ascending=[True,False,False]).head(30)
    grid_cols = st.columns(3, gap="medium")
    for i, (_, row) in enumerate(pick.iterrows()):
        with grid_cols[i % 3]:
            status = "rubi-bad" if row["health"] < 60 else ("rubi-good" if row["health"]>=80 else "rubi-warn")
            risks_list = [k.replace("flag_","").replace("_"," ") for k in [
                "flag_no_tasks","flag_no_company","flag_no_contact","flag_stuck"
            ] if row[k]]
            recs = deal_recommendations(row)
            st.markdown(f"""
            <div class="rubi-card">
              <div class="rubi-title">{row['TITLE']}</div>
              <div class="small">ID {row['ID']} • {row['manager']}</div>
              <hr/>
              <div class="rubi-chip {status}">Здоровье: <b>{row['health']}%</b></div>
              <div class="rubi-chip">Потенциал: <b>{row['potential']}%</b></div>
              <div class="rubi-chip">Сумма: <b>{int(row['OPPORTUNITY']):,} ₽</b></div>
              <div class="rubi-chip">Дней в работе: <b>{row['days_in_work']}</b></div>
              <div class="rubi-chip">Без активности: <b>{row['days_no_activity']} дн</b></div>
              <hr/>
              <div class="small">
                ⚠️ Риски: {", ".join(risks_list) or "нет"}<br/>
              </div>
              <div class="small">
                ✅ Рекомендации:<br/>• {"<br/>• ".join(recs)}
              </div>
            </div>
            """, unsafe_allow_html=True)

# --- ОТЧЁТ ПО СДЕЛКЕ (как в РУБИ)
with tab_deal:
    st.subheader("Отчёт по сделке (стиль РУБИ)")
    # выбор сделки
    options = view_df.sort_values("DATE_MODIFY", ascending=False)
    label_map = {int(r.ID): f"[{int(r.ID)}] {r.TITLE} — {r.manager}" for r in options[["ID","TITLE","manager"]].itertuples(index=False)}
    chosen_id = st.selectbox("Выберите сделку", list(label_map.keys()), format_func=lambda x: label_map[x])
    deal = view_df[view_df["ID"]==chosen_id].iloc[0]

    fin, lpr, need, timebox, main_task = report_texts(deal)
    comm = comm_scores(deal)
    act = activity_series(deal)

    # шапка
    a,b,c,d = st.columns([1.2,1,1,1])
    with a:
        st.markdown(f"#### {deal['TITLE']}")
        st.caption(f"Компания: {company_alias} • Ответственный: {deal['manager']}")
    with b:
        st.markdown(f"<div class='score-circle'>{deal['potential']}</div><div class='kpi-caption'>Потенциал сделки</div>", unsafe_allow_html=True)
    with c:
        st.markdown(f"<div class='score-circle'>{deal['health']}</div><div class='kpi-caption'>Здоровье сделки</div>", unsafe_allow_html=True)
    with d:
        st.markdown(f"<div class='kpi-number'>{int(deal['OPPORTUNITY']) if deal['OPPORTUNITY'] else 0}</div><div class='kpi-caption'>Сумма, ₽</div>", unsafe_allow_html=True)

    st.markdown("<br/>", unsafe_allow_html=True)
    # строки карточек
    col1, col2 = st.columns(2, gap="large")

    with col1:
        st.markdown("##### Общая информация")
        cgrid = st.container()
        with cgrid:
            st.markdown(f"""
            <div class="grid-2">
              <div class="rubi-card">
                <div class="rubi-title">Сумма сделки</div>
                <div class="kpi-number">{int(deal['OPPORTUNITY'])}</div>
                <div class="kpi-caption">вероятность {int(deal['PROBABILITY'])}%</div>
              </div>
              <div class="rubi-card">
                <div class="rubi-title">Дней в работе</div>
                <div class="kpi-number">{deal['days_in_work']}</div>
                <div class="kpi-caption">без активности {deal['days_no_activity']} дн</div>
              </div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("##### Контекст сделки")
        st.markdown(f"""
        <div class="grid-2">
          <div class="rubi-card"><div class="rubi-title">Финансовая готовность</div><div class="small">{fin}</div></div>
          <div class="rubi-card"><div class="rubi-title">Полномочие принятия решения</div><div class="small">{lpr}</div></div>
        </div>
        <div class="grid-2">
          <div class="rubi-card"><div class="rubi-title">Потребность и наш фокус</div><div class="small">{need}</div></div>
          <div class="rubi-card"><div class="rubi-title">Сроки и готовность к покупке</div><div class="small">{timebox}</div></div>
        </div>
        <div class="rubi-card"><div class="rubi-title">Задача</div><div class="small">Менеджеру: {main_task}.</div></div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown("##### Индикаторы и динамика")
        st.markdown(f"""
        <div class="grid-2">
          <div class="rubi-card">
            <div class="rubi-title">Потенциал сделки</div>
            <div class="score-circle">{deal['potential']}</div>
          </div>
          <div class="rubi-card">
            <div class="rubi-title">Здоровье сделки</div>
            <div class="score-circle">{deal['health']}</div>
          </div>
        </div>
        """, unsafe_allow_html=True)
        if px is None:
            st.info("Plotly недоступен — мини-график отключён.")
        else:
            fig = px.line(act, x="ts", y="activity")
            fig.update_yaxes(visible=False)
            fig.update_layout(margin=dict(l=8,r=8,t=4,b=4), height=220)
            st.markdown('<div class="rubi-card"><div class="rubi-title">Динамика работы со сделкой</div>', unsafe_allow_html=True)
            st.plotly_chart(fig, use_container_width=True)
            st.markdown('</div>', unsafe_allow_html=True)

        # Итоги работы + риски
        risks_list = [name for name,flag in {
            "без задач": deal["flag_no_tasks"], "без контактов": deal["flag_no_contact"],
            "без компании": deal["flag_no_company"], "застряла": deal["flag_stuck"]
        }.items() if flag]
        st.markdown(f"""
        <div class="rubi-card">
          <div class="rubi-title">Итоги работы</div>
          <div class="small">
            Этап: {deal['STAGE_ID'] or '—'}. Последняя активность: {str(deal['LAST_ACTIVITY_TIME'])[:19]}.<br/>
            Риски: {", ".join(risks_list) if risks_list else "существенных рисков не выявлено"}.
          </div>
        </div>
        """, unsafe_allow_html=True)

    # Оценка коммуникации
    st.markdown("##### Оценка коммуникации и рекомендации")
    g1, g2 = st.columns([1.2,1], gap="large")
    with g1:
        left, right = st.columns(2)
        items = list(comm.items())
        for i in range(0, len(items), 2):
            row_items = items[i:i+2]
            cols = st.columns(len(row_items))
            for (name, score), holder in zip(row_items, cols):
                with holder:
                    st.markdown(f"""
                    <div class="rubi-card">
                      <div class="rubi-title">{name}</div>
                      <div class="score-circle">{score}</div>
                    </div>
                    """, unsafe_allow_html=True)

    with g2:
        recs = deal_recommendations(deal)
        st.markdown(f"""
        <div class="rubi-card">
          <div class="rubi-title">План действий</div>
          <div class="small">• {"<br/>• ".join(recs)}</div>
        </div>
        """, unsafe_allow_html=True)

        # выгрузка рекомендаций в txt
        txt = f"""Отчёт по сделке: {deal['TITLE']} (ID {deal['ID']}) — {deal['manager']}
Сумма: {int(deal['OPPORTUNITY'])} ₽, Вероятность: {int(deal['PROBABILITY'])}%
Потенциал: {deal['potential']} / Здоровье: {deal['health']}
Риски: {", ".join([r for r in risks_list]) if risks_list else "нет"}

План действий:
- """ + "\n- ".join(recs)
        st.download_button("Скачать рекомендации (.txt)", data=txt.encode("utf-8"), file_name=f"deal_{int(deal['ID'])}_plan.txt")

# --- ЭКСПОРТ (CSV в ZIP)
with tab_export:
    st.subheader("Экспорт CSV (ZIP) — без Excel")

    # 01 — Сводка
    summary_df = pd.DataFrame({
        "Метрика": ["Всего сделок","Объём","Средн. здоровье","Застряли","Без задач","Без контактов","Без компаний","Потерянные"],
        "Значение": [
            view_df.shape[0],
            int(view_df["OPPORTUNITY"].sum()),
            f"{view_df['health'].mean():.0f}%",
            int(view_df["flag_stuck"].sum()),
            int((~view_df['ID'].isin(open_tasks_map.keys())).sum()),
            int(view_df["flag_no_contact"].sum()),
            int(view_df["flag_no_company"].sum()),
            int(view_df["flag_lost"].sum()),
        ]
    })

    # 02 — Менеджеры
    mgr_out = split_green_red(view_df)
    mgr_out["manager"] = mgr_out["ASSIGNED_BY_ID"].map(users_map).fillna("Неизвестно")
    mgr_out = mgr_out[["manager","deals","opp_sum","health_avg","no_tasks","stuck","lost","zone"]]

    # 03 — Сделки
    deal_cols = ["ID","TITLE","manager","STAGE_ID","OPPORTUNITY","PROBABILITY","health","potential",
                 "days_in_work","days_no_activity","flag_no_tasks","flag_no_contact","flag_no_company",
                 "flag_stuck","flag_lost","DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME"]
    deals_out = view_df[deal_cols].copy()

    def pack_zip_csv():
        mem = BytesIO()
        with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("01_summary.csv", summary_df.to_csv(index=False, encoding="utf-8-sig"))
            zf.writestr("02_managers.csv", mgr_out.to_csv(index=False, encoding="utf-8-sig"))
            zf.writestr("03_deals.csv", deals_out.to_csv(index=False, encoding="utf-8-sig"))
        mem.seek(0)
        return mem.getvalue()

    zip_bytes = pack_zip_csv()
    st.download_button(
        "Скачать отчёт (CSV.zip)",
        data=zip_bytes,
        file_name="rubi_like_report_csv.zip",
        mime="application/zip"
    )

# =========================
# ПОДВАЛ
# =========================
st.caption("RUBI-like Dashboard • автоаудит, пульс, менеджерские зоны, карточки, РУБИ-отчёт по сделке, экспорт CSV. v2.0")
