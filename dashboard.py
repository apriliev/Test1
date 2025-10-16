# -*- coding: utf-8 -*-
import os
import json
import time
import hashlib
from datetime import datetime, timedelta

import requests
import pandas as pd
import numpy as np
import streamlit as st
import plotly.express as px

# =========================
# НАСТРОЙКИ И СТИЛИ
# =========================
st.set_page_config(page_title="RUBI-like CRM Аналитика", page_icon="🧊", layout="wide")

CUSTOM_CSS = """
<style>
/* Брендинг в стиле RUBI */
:root { --rubi-accent:#6C5CE7; --rubi-red:#ff4d4f; --rubi-green:#22c55e; --rubi-yellow:#f59e0b; }
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
.rubi-card { border-radius:18px; padding:18px 18px 12px; background:#111418; border:1px solid #222; box-shadow:0 4px 18px rgba(0,0,0,.25); }
.rubi-title { font-weight:700; font-size:18px; margin-bottom:6px; }
.rubi-chip { display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:999px; border:1px solid #2a2f36; background:#0e1216; font-size:12px; }
.rubi-good { color: var(--rubi-green) !important; }
.rubi-bad  { color: var(--rubi-red) !important; }
.metric-row .stMetric { background:#0f1318; border:1px solid #262b33; border-radius:16px; padding:10px 12px; }
div[data-testid="stMetricValue"] { font-size:22px !important; }
.small { opacity:.8; font-size:12px; }
h1,h2,h3 { letter-spacing:.2px }
hr { border: 0; border-top:1px solid #222; margin: 12px 0 6px }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# =========================
# ПРОСТАЯ АВТОРИЗАЦИЯ
# =========================
def check_password():
    def password_entered():
        ok_user = st.session_state.get("username") in {"admin"}
        ok_pass = hashlib.sha256(st.session_state.get("password","").encode()).hexdigest() \
                  == "240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9"
        st.session_state["password_correct"] = bool(ok_user and ok_pass)
        st.session_state.pop("password", None)

    if "password_correct" not in st.session_state or not st.session_state["password_correct"]:
        st.markdown("### 🔐 Вход в систему")
        st.text_input("Логин", key="username")
        st.text_input("Пароль", type="password", key="password", on_change=password_entered)
        st.stop()

check_password()
with st.sidebar:
    if st.button("Выйти"):
        st.session_state["password_correct"] = False
        st.experimental_rerun()

# =========================
# СЕКРЕТЫ
# =========================
BITRIX24_WEBHOOK = os.getenv("BITRIX24_WEBHOOK")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"

if not BITRIX24_WEBHOOK:
    st.error("❌ Укажи BITRIX24_WEBHOOK в Secrets/переменных окружения")
    st.stop()

# =========================
# ХЕЛПЕРЫ ДЛЯ API
# =========================
def bx_get(method, params=None, pause=0.4):
    """Безопасный GET к Bitrix с авто-пагинацией по 50"""
    url = BITRIX24_WEBHOOK.rstrip("/") + f"/{method}.json"
    out, start = [], 0
    params = dict(params or {})
    while True:
        params["start"] = start
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        res = data.get("result")
        if isinstance(res, dict) and "items" in res:  # некоторые методы отдают items
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
def get_deals(date_from=None, date_to=None, limit=1000):
    filt = {}
    if date_from: filt["filter[>=DATE_CREATE]"] = date_from
    if date_to:   filt["filter[<=DATE_CREATE]"] = date_to
    params = {"select[]":[
        "ID","TITLE","STAGE_ID","OPPORTUNITY","ASSIGNED_BY_ID",
        "COMPANY_ID","CONTACT_ID","PROBABILITY",
        "DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME"
    ], **filt}
    deals = bx_get("crm.deal.list", params)
    deals = deals[:limit]
    return deals

@st.cache_data(show_spinner=False, ttl=300)
def get_users():
    users = bx_get("user.get", {})
    return {int(u["ID"]): (u.get("NAME","")+ " " + u.get("LAST_NAME","")).strip() or u.get("LOGIN", "") for u in users}

@st.cache_data(show_spinner=False, ttl=300)
def get_open_activities_for_deal_ids(deal_ids):
    """Открытые активити по сделкам: 0 = нет задач → 'без задач'"""
    out = {}
    if not deal_ids: return out
    for chunk in np.array_split(list(map(int, deal_ids)), max(1, len(deal_ids)//40 + 1)):
        params = {
            "filter[OWNER_TYPE_ID]": 2,  # 2 = Deal
            "filter[OWNER_ID]": ",".join(map(str, chunk)),
            "filter[COMPLETED]": "N"
        }
        acts = bx_get("crm.activity.list", params)
        for a in acts:
            k = int(a["OWNER_ID"])
            out.setdefault(k, []).append(a)
    return out

# =========================
# ЛОГИКА ОЦЕНОК
# =========================
def to_dt(x):
    try:
        return pd.to_datetime(x)
    except:
        return pd.NaT

def compute_health_scores(df, open_tasks_map, stuck_days=5):
    """Рассчитываем здоровье/потенциал/флаги проблем для каждой сделки"""
    now = pd.Timestamp.utcnow()
    records = []
    for _, r in df.iterrows():
        last = to_dt(r["LAST_ACTIVITY_TIME"]) or to_dt(r["DATE_MODIFY"]) or to_dt(r["DATE_CREATE"])
        days_in_work = max(0, (now - to_dt(r["DATE_CREATE"])).days if pd.notna(to_dt(r["DATE_CREATE"])) else 0)
        days_no_activity = (now - (last if pd.notna(last) else now)).days
        has_task = len(open_tasks_map.get(int(r["ID"]), [])) > 0

        flags = {
            "no_company": int(r.get("COMPANY_ID") or 0) == 0,
            "no_contact": int(r.get("CONTACT_ID") or 0) == 0,
            "no_tasks": not has_task,
            "stuck": days_no_activity >= stuck_days,
            "lost": isinstance(r.get("STAGE_ID"), str) and ("LOSE" in r["STAGE_ID"] or "LOSE" in r["STAGE_ID"].upper())
        }

        # Правила скоринга (простые и наглядные)
        score = 100
        if flags["no_company"]: score -= 10
        if flags["no_contact"]: score -= 10
        if flags["no_tasks"]:   score -= 25
        if flags["stuck"]:      score -= 25
        if flags["lost"]:       score = min(score, 15)

        # Потенциал = нормализуем объём и вероятность
        opp = float(r.get("OPPORTUNITY") or 0)
        prob = float(r.get("PROBABILITY") or 0)
        potential = min(100, int((opp > 0) * (30 + min(70, np.log10(max(1, opp))/5 * 70)) * (0.4 + prob/100*0.6)))

        records.append({
            "ID": int(r["ID"]),
            "TITLE": r["TITLE"],
            "ASSIGNED_BY_ID": int(r.get("ASSIGNED_BY_ID") or 0),
            "STAGE_ID": r.get("STAGE_ID",""),
            "OPPORTUNITY": opp,
            "PROBABILITY": prob,
            "DATE_CREATE": to_dt(r["DATE_CREATE"]),
            "DATE_MODIFY": to_dt(r["DATE_MODIFY"]),
            "LAST_ACTIVITY_TIME": last,
            "days_in_work": days_in_work,
            "days_no_activity": days_no_activity,
            "health": max(0, min(100, int(score))),
            "potential": max(0, min(100, int(potential))),
            **{f"flag_{k}": v for k, v in flags.items()}
        })
    return pd.DataFrame(records)

def split_green_red(manager_df):
    """Зелёная/красная зоны по менеджерам: комбинированный индекс"""
    g = manager_df.copy()
    # Больше здоровья, меньше проблем — лучше
    g["problem_index"] = (
        g["flag_no_tasks"].sum(level=0) if isinstance(g.index, pd.MultiIndex) else 0
    )
    g["score"] = (
        g["health"].mean(level=0) if isinstance(g.index, pd.MultiIndex) else 0
    )
    # Упростим:
    grp = g.groupby("ASSIGNED_BY_ID").agg(
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

# =========================
# ФУНКЦИИ AI-РЕЗЮМЕ
# =========================
def ai_sumarize(company_name, df_summary, df_managers, examples=4):
    if not PERPLEXITY_API_KEY:  # работаем без внешнего ИИ, если ключа нет
        return "AI-резюме недоступно: нет PERPLEXITY_API_KEY.", []

    sample_deals = df_summary.head(examples)[[
        "ID","TITLE","health","potential","OPPORTUNITY","days_in_work",
        "flag_no_tasks","flag_stuck","flag_no_company","flag_no_contact","flag_lost"
    ]].to_dict(orient="records")

    payload = {
        "model": "sonar-pro",
        "messages": [
            {"role":"system","content":"Отвечай строго валидным JSON с ключами: summary (строка), actions (список коротких пунктов)."},
            {"role":"user","content": json.dumps({
                "company": company_name,
                "kpi_summary": df_managers.describe(include="all").to_dict(),
                "sample_deals": sample_deals
            }, ensure_ascii=False)}
        ],
        "temperature": 0.1,
        "max_tokens": 800
    }
    r = requests.post(PERPLEXITY_API_URL, headers={"Authorization":f"Bearer {PERPLEXITY_API_KEY}"}, json=payload, timeout=60)
    txt = r.json().get("choices",[{}])[0].get("message",{}).get("content","")
    i,j = txt.find("{"), txt.rfind("}")+1
    try:
        data = json.loads(txt[i:j])
        return data.get("summary",""), data.get("actions",[])
    except Exception:
        return "Не удалось сформировать AI-резюме.", []

# =========================
# ФИЛЬТРЫ
# =========================
st.sidebar.title("Фильтры")
company_alias = st.sidebar.text_input("Компания (название для отчёта)", "ООО «Фокус»")
date_from = st.sidebar.date_input("С какой даты", datetime.now().date() - timedelta(days=30))
date_to   = st.sidebar.date_input("По какую дату", datetime.now().date())
stuck_days = st.sidebar.slider("Считать «застряла», если нет активности дней", 2, 21, 5)
limit = st.sidebar.slider("Лимит сделок для выборки", 50, 3000, 600, step=50)

# =========================
# ЗАГРУЗКА ДАННЫХ
# =========================
with st.spinner("Загружаю сделки и активные задачи из Bitrix24…"):
    deals_raw = get_deals(str(date_from), str(date_to), limit=limit)
    if not deals_raw:
        st.warning("За выбранный период сделок не найдено.")
        st.stop()

    df = pd.DataFrame(deals_raw)
    df["OPPORTUNITY"] = pd.to_numeric(df["OPPORTUNITY"], errors="coerce").fillna(0.0)
    users_map = get_users()
    open_tasks_map = get_open_activities_for_deal_ids(df["ID"].tolist())
    df_scores = compute_health_scores(df, open_tasks_map, stuck_days=stuck_days)
    df_scores["manager"] = df_scores["ASSIGNED_BY_ID"].map(users_map).fillna("Неизвестно")
    mgr = split_green_red(df_scores)

st.title("RUBI-style Контроль отдела продаж")
st.caption("Автоаудит воронки • Пульс сделок • Зелёная/красная зоны • Карточки с рекомендациями • Экспорт в Excel")

# =========================
# ВЕРХНИЕ МЕТРИКИ
# =========================
col1,col2,col3,col4,col5 = st.columns(5, gap="small")
with col1: st.metric("Всего сделок", int(df_scores.shape[0]))
with col2: st.metric("Объём, ₽", f"{int(df_scores['OPPORTUNITY'].sum()):,}".replace(","," "))
with col3: st.metric("Средний чек, ₽", f"{int(df_scores['OPPORTUNITY'].replace(0,np.nan).mean() or 0):,}".replace(","," "))
with col4: st.metric("Средн. здоровье", f"{df_scores['health'].mean():.0f}%")
with col5: st.metric("Потенциал (сумма)", int(df_scores["potential"].sum()))

# =========================
# ВКЛАДКИ
# =========================
tab_pulse, tab_audit, tab_managers, tab_cards, tab_export = st.tabs([
    "⛵ Пульс сделок", "🚁 Аудит воронки", "🚀 Результаты ОП", "🗂 Карточки сделок", "⬇️ Экспорт"
])

# --- ПУЛЬС
with tab_pulse:
    c1,c2 = st.columns([3,2], gap="large")
    with c1:
        st.subheader("Динамика по этапам")
        fig = px.bar(
            df_scores.groupby("STAGE_ID").agg(Сумма=("OPPORTUNITY","sum"), Количество=("ID","count")).reset_index(),
            x="STAGE_ID", y="Сумма", text="Количество"
        )
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        st.subheader("Распределение здоровья")
        fig2 = px.histogram(df_scores, x="health", nbins=20)
        st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Лента сделок (последние изменения)")
    st.dataframe(
        df_scores.sort_values("DATE_MODIFY", ascending=False)[
            ["ID","TITLE","manager","STAGE_ID","OPPORTUNITY","health","potential","DATE_MODIFY"]
        ].head(200),
        height=360
    )

# --- АУДИТ
with tab_audit:
    st.subheader("Проблемные зоны")
    kpis = {
        "Сделок без задач": int((~df_scores["ID"].isin(open_tasks_map.keys())).sum()),
        "Сделок без контактов": int(df_scores["flag_no_contact"].sum()),
        "Сделок без компаний": int(df_scores["flag_no_company"].sum()),
        "Застрявшие сделки": int(df_scores["flag_stuck"].sum()),
        "Потерянные сделки": int(df_scores["flag_lost"].sum()),
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
        ("Без задач", ~df_scores["ID"].isin(open_tasks_map.keys())),
        ("Без контактов", df_scores["flag_no_contact"]),
        ("Без компаний", df_scores["flag_no_company"]),
        ("Застряли", df_scores["flag_stuck"]),
        ("Потерянные", df_scores["flag_lost"]),
    ]
    for (title, mask), holder in zip(lists, cols):
        with holder:
            st.markdown(f'<div class="rubi-card"><div class="rubi-title">{title}</div>', unsafe_allow_html=True)
            st.dataframe(df_scores[mask][["ID","TITLE","manager","STAGE_ID","OPPORTUNITY","health","days_no_activity"]].head(50), height=280)
            st.markdown("</div>", unsafe_allow_html=True)

# --- МЕНЕДЖЕРЫ
with tab_managers:
    st.subheader("Зелёная / Красная зоны по менеджерам")
    mgr["manager"] = mgr["ASSIGNED_BY_ID"].map(users_map).fillna("Неизвестно")
    left, right = st.columns([1.4,1], gap="large")

    with left:
        fig = px.scatter(
            mgr, x="health_avg", y="no_tasks", size="opp_sum", color="zone",
            hover_data=["manager","deals","stuck","lost","potential_sum"],
            labels={"health_avg":"Средн. здоровье","no_tasks":"Без задач (шт)"}
        )
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("**Таблица менеджеров**")
        st.dataframe(
            mgr[["manager","deals","opp_sum","health_avg","no_tasks","stuck","lost","zone"]].sort_values(["zone","health_avg"], ascending=[True,False]),
            height=360
        )

    with right:
        st.markdown("#### Лидеры и рисковые")
        top = df_scores.groupby("manager").agg(
            deals=("ID","count"),
            health_avg=("health","mean"),
            opp=("OPPORTUNITY","sum"),
            stuck=("flag_stuck","sum"),
            no_tasks=("flag_no_tasks","sum"),
            lost=("flag_lost","sum"),
        ).reset_index()

        st.markdown("**Зелёная зона**")
        st.dataframe(top.query("health_avg>=70").sort_values("health_avg", ascending=False).head(10), height=180)
        st.markdown("**Красная зона**")
        st.dataframe(top.query("health_avg<70 or no_tasks>2 or stuck>2").sort_values(["health_avg","no_tasks","stuck"], ascending=[True,False,False]).head(10), height=180)

# --- КАРТОЧКИ СДЕЛОК
with tab_cards:
    st.subheader("Карточки с оценкой и планом")
    pick_manager = st.multiselect("Фильтр по менеджерам", sorted(df_scores["manager"].unique()), default=[])
    pick = df_scores[df_scores["manager"].isin(pick_manager)] if pick_manager else df_scores
    pick = pick.sort_values(["health","potential","OPPORTUNITY"], ascending=[True,False,False]).head(30)

    grid_cols = st.columns(3, gap="medium")
    for i, (_, row) in enumerate(pick.iterrows()):
        with grid_cols[i % 3]:
            status = "rubi-bad" if row["health"] < 60 else ("rubi-good" if row["health"]>=80 else "")
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
                ⚠️ Риски: {", ".join([k.replace("flag_","").replace("_"," ") for k,v in row.items() if k.startswith("flag_") and v and k not in ["flag_lost"]]) or "нет"}<br/>
                ❌ Потеряна: {"да" if row["flag_lost"] else "нет"}<br/>
              </div>
            </div>
            """, unsafe_allow_html=True)

# --- ЭКСПОРТ
with tab_export:
    st.subheader("Формирование XLS-отчёта (в стиле РУБИ)")
    def build_excel_bytes():
        from io import BytesIO
        bio = BytesIO()
        with pd.ExcelWriter(bio, engine="xlsxwriter") as xw:
            # Сводка
            summary = pd.DataFrame({
                "Метрика": ["Всего сделок","Объём","Средн. здоровье","Застряли","Без задач","Без контактов","Без компаний","Потерянные"],
                "Значение": [
                    df_scores.shape[0],
                    int(df_scores["OPPORTUNITY"].sum()),
                    f"{df_scores['health'].mean():.0f}%",
                    int(df_scores["flag_stuck"].sum()),
                    int((~df_scores['ID'].isin(open_tasks_map.keys())).sum()),
                    int(df_scores["flag_no_contact"].sum()),
                    int(df_scores["flag_no_company"].sum()),
                    int(df_scores["flag_lost"].sum()),
                ]
            })
            summary.to_excel(xw, sheet_name="01_Сводка", index=False)

            # Менеджеры
            mgr_out = mgr[["manager","deals","opp_sum","health_avg","no_tasks","stuck","lost","zone"]]
            mgr_out.to_excel(xw, sheet_name="02_Менеджеры", index=False)

            # Детализация сделок
            detail_cols = ["ID","TITLE","manager","STAGE_ID","OPPORTUNITY","PROBABILITY","health","potential","days_in_work","days_no_activity",
                           "flag_no_tasks","flag_no_contact","flag_no_company","flag_stuck","flag_lost","DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME"]
            df_scores[detail_cols].to_excel(xw, sheet_name="03_Сделки", index=False)

        bio.seek(0)
        return bio.getvalue()

    xls_bytes = build_excel_bytes()
    st.download_button("Скачать XLS-отчёт", data=xls_bytes, file_name="rubi_like_report.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# =========================
# AI-КРАТКОЕ РЕЗЮМЕ И ПЛАН
# =========================
st.markdown("### 🔮 AI-Резюме и план действий")
if st.button("Сформировать краткий обзор"):
    with st.spinner("Готовлю AI-резюме по текущим данным…"):
        text, actions = ai_sumarize(company_alias, df_scores, mgr)
    st.info(text or "—")
    if actions:
        st.markdown("**Рекомендуемые шаги:**")
        for a in actions:
            st.write(f"• {a}")
    else:
        st.caption("Нет действий / ИИ недоступен.")

# =========================
# ПОДВАЛ
# =========================
st.caption("Вдохновлено РУБИ ЧАТ: автоаудит, пульс сделок, менеджерские зоны, карточки, экспорт. Версия 1.0")
