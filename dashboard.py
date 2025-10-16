# -*- coding: utf-8 -*-
"""
RUBI-like CRM Dashboard (Streamlit, no-Excel build)
- Пульс воронки, аудит, карточки сделок, зелёная/красная зоны по менеджерам
- Источник данных: Bitrix24 (webhook) или офлайн-таблица (CSV/XLSX)
- Экспорт: ZIP с CSV-файлами (без Excel-зависимостей)
"""

import os
import json
import time
import hashlib
from datetime import datetime, timedelta
from io import BytesIO
import zipfile

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
.block-container { padding-top: 1.0rem; padding-bottom: 1.2rem; }
.rubi-card { border-radius:18px; padding:18px 18px 12px; background:#111418; border:1px solid #222; box-shadow:0 4px 18px rgba(0,0,0,.25); }
.rubi-title { font-weight:700; font-size:18px; margin-bottom:6px; }
.rubi-chip { display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:999px; border:1px solid #2a2f36; background:#0e1216; font-size:12px; margin-right:6px; margin-bottom:6px;}
.rubi-good { color: var(--rubi-green) !important; }
.rubi-bad  { color: var(--rubi-red) !important; }
.small { opacity:.8; font-size:12px; }
hr { border: 0; border-top:1px solid #222; margin: 10px 0 6px }
div[data-testid="stMetricValue"] { font-size:22px !important; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# =========================
# ПРОСТАЯ АВТОРИЗАЦИЯ
# логин: admin
# пароль: 123  (можешь заменить хэш ниже)
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
        potential = min(100, int((opp > 0) * (30 + min(70, np.log10(max(1, opp))/5 * 70)) * (0.4 + prob/100*0.6)))

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

def ai_summarize(company_name, df_summary, df_managers, api_key, api_url):
    """Короткое резюме + план (Perplexity). Без ключа — возвращает stub."""
    if not api_key:
        return "AI-резюме недоступно (нет API-ключа).", []
    try:
        import requests
        sample = df_summary.sort_values("health").head(4)[[
            "ID","TITLE","health","potential","OPPORTUNITY","days_in_work",
            "flag_no_tasks","flag_stuck","flag_no_company","flag_no_contact","flag_lost"
        ]].to_dict(orient="records")
        payload = {
            "model": "sonar-pro",
            "messages": [
                {"role":"system","content":"Отвечай строго валидным JSON с ключами: summary (string), actions (string[])."},
                {"role":"user","content": json.dumps({
                    "company": company_name,
                    "kpi_summary": df_managers.describe(include="all").to_dict(),
                    "sample_deals": sample
                }, ensure_ascii=False)}
            ],
            "temperature": 0.1,
            "max_tokens": 800
        }
        r = requests.post(api_url, headers={"Authorization":f"Bearer {api_key}"}, json=payload, timeout=60)
        txt = r.json().get("choices",[{}])[0].get("message",{}).get("content","")
        i,j = txt.find("{"), txt.rfind("}")+1
        data = json.loads(txt[i:j]) if i>=0 and j>i else {}
        return data.get("summary","Не удалось разобрать ответ."), data.get("actions",[])
    except Exception:
        return "Не удалось сформировать AI-резюме.", []

# =========================
# БОКОВАЯ ПАНЕЛЬ / ФИЛЬТРЫ
# =========================
st.sidebar.title("Фильтры")
company_alias = st.sidebar.text_input("Компания (в шапке отчёта)", "ООО «Фокус»")
date_from = st.sidebar.date_input("С какой даты", datetime.now().date() - timedelta(days=30))
date_to   = st.sidebar.date_input("По какую дату", datetime.now().date())
stuck_days = st.sidebar.slider("Нет активности ≥ (дней)", 2, 21, 5)
limit = st.sidebar.slider("Лимит сделок (API)", 50, 3000, 600, step=50)

# Офлайн-файл (если нет вебхука)
uploaded_offline = None
if not BITRIX24_WEBHOOK:
    st.sidebar.warning("BITRIX24_WEBHOOK не задан — доступен офлайн-режим (загрузите CSV/XLSX).")
    uploaded_offline = st.sidebar.file_uploader("Загрузить CSV/XLSX со сделками", type=["csv","xlsx"])

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
    mgr = split_green_red(df_scores)

# =========================
# ВЕРХ ШАПКИ
# =========================
st.title("RUBI-style Контроль отдела продаж")
st.caption("Автоаудит воронки • Пульс сделок • Зоны менеджеров • Карточки • Экспорт CSV")

# Топ-метрики
c1,c2,c3,c4,c5 = st.columns(5, gap="small")
with c1: st.metric("Всего сделок", int(df_scores.shape[0]))
with c2: st.metric("Объём, ₽", f"{int(df_scores['OPPORTUNITY'].sum()):,}".replace(","," "))
with c3: st.metric("Средний чек, ₽", f"{int(df_scores['OPPORTUNITY'].replace(0,np.nan).mean() or 0):,}".replace(","," "))
with c4: st.metric("Средн. здоровье", f"{df_scores['health'].mean():.0f}%")
with c5: st.metric("Суммарный потенциал", int(df_scores["potential"].sum()))

# =========================
# ВКЛАДКИ
# =========================
tab_pulse, tab_audit, tab_managers, tab_cards, tab_export = st.tabs([
    "⛵ Пульс сделок", "🚁 Аудит воронки", "🚀 Менеджеры", "🗂 Карточки", "⬇️ Экспорт (CSV)"
])

# --- ПУЛЬС
with tab_pulse:
    if px is None:
        st.warning("Plotly недоступен — графики отключены.")
    else:
        a,b = st.columns([3,2], gap="large")
        with a:
            st.subheader("Динамика по этапам")
            stage_df = df_scores.groupby("STAGE_ID").agg(Сумма=("OPPORTUNITY","sum"), Количество=("ID","count")).reset_index()
            fig = px.bar(stage_df, x="STAGE_ID", y="Сумма", text="Количество")
            st.plotly_chart(fig, use_container_width=True)
        with b:
            st.subheader("Распределение здоровья")
            fig2 = px.histogram(df_scores, x="health", nbins=20)
            st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Лента изменений (последние)")
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
            st.dataframe(
                df_scores[mask][["ID","TITLE","manager","STAGE_ID","OPPORTUNITY","health","days_no_activity"]].head(80),
                height=260
            )
            st.markdown("</div>", unsafe_allow_html=True)

# --- МЕНЕДЖЕРЫ
with tab_managers:
    st.subheader("Зелёная / Красная зоны по менеджерам")
    mgr = mgr.copy()
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
        agg = df_scores.groupby("manager").agg(
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
    st.subheader("Карточки с оценкой и планом")
    pick_manager = st.multiselect("Фильтр по менеджерам", sorted(df_scores["manager"].unique()), default=[])
    pick = df_scores[df_scores["manager"].isin(pick_manager)] if pick_manager else df_scores
    pick = pick.sort_values(["health","potential","OPPORTUNITY"], ascending=[True,False,False]).head(30)

    grid_cols = st.columns(3, gap="medium")
    for i, (_, row) in enumerate(pick.iterrows()):
        with grid_cols[i % 3]:
            status = "rubi-bad" if row["health"] < 60 else ("rubi-good" if row["health"]>=80 else "")
            risks_list = [k.replace("flag_","").replace("_"," ") for k in [
                "flag_no_tasks","flag_no_company","flag_no_contact","flag_stuck"
            ] if row[k]]
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
                ❌ Потеряна: {"да" if row["flag_lost"] else "нет"}<br/>
              </div>
            </div>
            """, unsafe_allow_html=True)

# --- ЭКСПОРТ (CSV в ZIP)
with tab_export:
    st.subheader("Экспорт CSV (ZIP) — работает без Excel")

    # 01 — Сводка
    summary_df = pd.DataFrame({
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

    # 02 — Менеджеры
    mgr_out = split_green_red(df_scores)
    mgr_out["manager"] = mgr_out["ASSIGNED_BY_ID"].map(users_map).fillna("Неизвестно")
    mgr_out = mgr_out[["manager","deals","opp_sum","health_avg","no_tasks","stuck","lost","zone"]]

    # 03 — Сделки
    deal_cols = ["ID","TITLE","manager","STAGE_ID","OPPORTUNITY","PROBABILITY","health","potential",
                 "days_in_work","days_no_activity","flag_no_tasks","flag_no_contact","flag_no_company",
                 "flag_stuck","flag_lost","DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME"]
    deals_out = df_scores[deal_cols].copy()

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
# AI-КРАТКОЕ РЕЗЮМЕ
# =========================
st.markdown("### 🔮 AI-резюме и план действий")
if st.button("Сформировать краткий обзор"):
    with st.spinner("Формирую AI-обзор…"):
        text, actions = ai_summarize(company_alias, df_scores, mgr, PERPLEXITY_API_KEY, PERPLEXITY_API_URL)
    st.info(text or "—")
    if actions:
        st.markdown("**Шаги:**")
        for a in actions:
            st.write(f"• {a}")
    else:
        st.caption("Нет рекомендаций / ИИ недоступен.")

# =========================
# ПОДВАЛ
# =========================
st.caption("RUBI-like Dashboard • автоаудит, пульс, менеджерские зоны, карточки, экспорт CSV. v1.2 (no-Excel)")
