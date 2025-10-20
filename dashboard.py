# -*- coding: utf-8 -*-
"""
БУРМАШ · CRM Дэшборд v4.2
С AI-аналитикой, метриками проблем и годовым планом
"""
import os, time, math, json
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import streamlit as st
import requests

try:
    import plotly.express as px
    import plotly.graph_objects as go
except:
    px = go = None

# ============ CONFIG ============
st.set_page_config(page_title="БУРМАШ · CRM", page_icon="🟧", layout="wide")

def check_password():
    def password_entered():
        st.session_state["password_correct"] = (
            st.session_state.get("username") == "admin" and
            st.session_state.get("password") == "admin123"
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

def get_secret(name, default=None):
    return st.secrets.get(name) or os.getenv(name, default) or default

BITRIX24_WEBHOOK = (get_secret("BITRIX24_WEBHOOK", "") or "").strip()
PERPLEXITY_API_KEY = (get_secret("PERPLEXITY_API_KEY", "") or "").strip()

# ============ BITRIX HELPERS ============
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
def bx_get_deals(date_from=None, date_to=None, limit=1000):
    params = {"select[]":[
        "ID","TITLE","STAGE_ID","OPPORTUNITY","ASSIGNED_BY_ID","COMPANY_ID","CONTACT_ID",
        "PROBABILITY","DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME","CATEGORY_ID","BEGINDATE"
    ]}
    if date_from: params["filter[>=DATE_CREATE]"] = date_from
    if date_to: params["filter[<=DATE_CREATE]"] = date_to
    deals = _bx_get("crm.deal.list", params)
    return deals[:limit]

@st.cache_data(ttl=300)
def bx_get_users_full():
    users = _bx_get("user.get", {})
    out = {}
    for u in users:
        depts = u.get("UF_DEPARTMENT") or []
        if isinstance(depts, str): depts = [int(x) for x in depts.split(",") if x]
        out[int(u["ID"])] = {
            "name": ((u.get("NAME","")+u.get("LAST_NAME","")).strip() or u.get("LOGIN","")).strip(),
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

# ============ AI ANALYSIS ============
def ai_analyze_manager(manager_name, deals_summary):
    if not PERPLEXITY_API_KEY:
        return "AI-ключ не настроен."
    prompt = f"""
Ты эксперт по продажам и CRM-аналитике. Проанализируй работу менеджера по продажам.

Менеджер: {manager_name}
Данные: {json.dumps(deals_summary, ensure_ascii=False)}

Дай краткий анализ (2-3 абзаца):
1. Сильные стороны и что работает хорошо
2. Проблемные зоны и западания
3. Конкретные рекомендации для улучшения показателей

Пиши на русском, деловым стилем.
"""
    data = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": "Ты эксперт по CRM-аналитике."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 800,
        "temperature": 0.3
    }
    try:
        resp = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {PERPLEXITY_API_KEY}"},
            json=data,
            timeout=30
        )
        result = resp.json()
        return result["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка AI-анализа: {str(e)}"

# ============ UTILS ============
def to_dt(x):
    try:
        ts = pd.to_datetime(x, utc=True, errors="coerce")
        if pd.isna(ts): return pd.NaT
        return ts.tz_convert(None)
    except: return pd.NaT

def days_between(later, earlier):
    a, b = to_dt(later), to_dt(earlier)
    if pd.isna(a) or pd.isna(b): return None
    return max(0, int((a - b) / pd.Timedelta(days=1)))

# ============ SCORING ============
def compute_health_scores(df, open_tasks_map, stuck_days=5):
    now = to_dt(pd.Timestamp.utcnow())
    rows = []
    for _, r in df.iterrows():
        create_dt = to_dt(r.get("DATE_CREATE"))
        last = to_dt(r.get("LAST_ACTIVITY_TIME")) or to_dt(r.get("DATE_MODIFY")) or create_dt
        begin_dt = to_dt(r.get("BEGINDATE")) or create_dt
        d_work = days_between(now, create_dt) or 0
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
        if flags["no_tasks"]: score -= 25
        if flags["stuck"]: score -= 25
        if flags["lost"]: score = min(score, 15)
        opp = float(r.get("OPPORTUNITY") or 0.0)
        prob = float(r.get("PROBABILITY") or 0.0)
        potential = min(100, int((opp > 0) * (30 + min(70, math.log10(max(1, opp))/5 * 70)) * (0.4 + prob/100 * 0.6)))
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
    if df.empty:
        return df.assign(Скор_быстрой_победы=0.0, ETA_дней=np.nan, Скор_отказа=0.0)
    eps = 1e-9
    prob = df["Вероятность"].clip(0, 100) / 100.0
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
    quick = (0.35*prob + 0.25*health + 0.15*recency + 0.15*stage_closeness + 0.10*opp_norm)
    quick_score = (quick*100).round(1)
    eta = (30*(1-stage_closeness)*(1 - 0.5*health - 0.5*prob)).clip(lower=0)
    eta_days = eta.round(0)
    age_norm = (df["Дней в работе"]/max(df["Дней в работе"].max(),1)).clip(0,1)
    noact_norm = (df["Дней без активности"]/max(df["Дней без активности"].max(),1)).clip(0,1)
    drop = (1-prob)*0.4 + (1-health)*0.3 + noact_norm*0.2 + age_norm*0.1
    drop_score = (drop*100).round(1)
    out = df.copy()
    out["Скор быстрой победы"] = quick_score
    out["ETA дней"] = eta_days
    out["Скор отказа"] = drop_score
    out["Быстрая победа?"] = (out["Скор быстрой победы"]>=60) & (out["Вероятность"]>=min_prob) & (~out["Проиграна"])
    out["Стоп-лист?"] = (out["Скор отказа"]>=70) | (out["Проиграна"]) | ((out["Здоровье"]<40) & (out["Дней без активности"]>horizon_days))
    return out

def compute_conversion_by_manager_and_funnel(df, sort_map):
    results = []
    for (mgr, cat), g in df.groupby(["Менеджер", "Воронка"], dropna=False):
        stages_sorted = sorted(g["Этап ID"].unique(), key=lambda s: sort_map.get(str(s), 9999))
        stage_counts = g.groupby("Этап ID").size()
        total = len(g)
        stage_data = []
        for s in stages_sorted:
            cnt = stage_counts.get(s, 0)
            conv = (cnt / total * 100) if total > 0 else 0
            stage_data.append({"Этап": s, "Количество": cnt, "Конверсия %": round(conv, 1)})
        results.append({
            "Менеджер": mgr,
            "Воронка": cat,
            "Всего сделок": total,
            "Этапы": stage_data
        })
    return pd.DataFrame(results)

# ============ SIDEBAR ============
st.sidebar.title("Фильтры")
date_from = st.sidebar.date_input("С даты", datetime.now().date() - timedelta(days=30))
date_to   = st.sidebar.date_input("По дату", datetime.now().date())
stuck_days= st.sidebar.slider("Нет активности ≥ (дней)", 2, 21, 5)
limit     = st.sidebar.slider("Лимит сделок (API)", 50, 3000, 600, step=50)

st.sidebar.title("Настройки фокуса РОПа")
focus_horizon = st.sidebar.slider("Горизонт фокуса (дней)", 7, 45, 14)
focus_min_prob= st.sidebar.slider("Мин. вероятность для фокуса, %", 0, 100, 50)

# ============ LOAD DATA ============
with st.spinner("Загружаю данные…"):
    if not BITRIX24_WEBHOOK:
        st.error("Задайте BITRIX24_WEBHOOK в Secrets")
        st.stop()
    deals_raw = bx_get_deals(str(date_from), str(date_to), limit=limit)
    if not deals_raw:
        st.error("Сделок не найдено за выбранный период.")
        st.stop()
    df_raw = pd.DataFrame(deals_raw)
    df_raw["OPPORTUNITY"] = pd.to_numeric(df_raw.get("OPPORTUNITY"), errors="coerce").fillna(0.0)
    users_full = bx_get_users_full()
    users_map = {uid: users_full[uid]["name"] for uid in users_full}
    open_tasks_map = bx_get_open_activities_for_deal_ids(df_raw["ID"].tolist())
    categories_map   = bx_get_categories()

df_scores = compute_health_scores(df_raw, open_tasks_map, stuck_days=stuck_days)
stage_ids = df_scores["Этап ID"].dropna().unique().tolist()
sort_map, name_map = bx_get_stage_map(stage_ids)
FALLBACK_ORDER = ["NEW","NEW_LEAD","PREPARATION","PREPAYMENT_INVOICE","EXECUTING","FINAL_INVOICE","WON","LOSE"]
def fallback_sort(sid):
    sid = str(sid or "")
    sid_short = sid.split(":")[1] if ":" in sid else sid
    return (FALLBACK_ORDER.index(sid_short)*100 if sid_short in FALLBACK_ORDER else 10000 + hash(sid_short)%1000)
df_scores["Сортировка этапа"] = df_scores["Этап ID"].map(lambda s: sort_map.get(str(s), fallback_sort(s)))
df_scores["Название этапа"] = df_scores["Этап ID"].map(lambda s: name_map.get(str(s), str(s)))
df_scores["Менеджер"] = df_scores["Менеджер ID"].map(users_map).fillna("Неизвестно")
df_scores["Воронка"]  = df_scores["Воронка ID"].map(lambda x: categories_map.get(int(x or 0), "Основная"))
df_scores = focus_scores(df_scores, horizon_days=focus_horizon, min_prob=focus_min_prob)

funnels  = sorted(df_scores["Воронка"].unique())
selected_funnels  = st.sidebar.multiselect("Воронки", funnels, default=funnels)
managers = sorted(df_scores["Менеджер"].unique())
selected_managers = st.sidebar.multiselect("Менеджеры", managers, default=managers)

view_df = df_scores[
    (df_scores["Воронка"].isin(selected_funnels)) &
    (df_scores["Менеджер"].isin(selected_managers))
].copy()
if view_df.empty:
    st.warning("Нет данных по выбранным фильтрам.")
    st.stop()

# ============ HEADER ============
st.markdown("# 🟧 БУРМАШ · CRM Дэшборд v4.2")
st.markdown(f"**Период**: {date_from} → {date_to} | **Сделок**: {len(view_df)}")

# ============ TABS ============
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📊 Обзор",
    "⚠️ Проблемы",
    "👤 По менеджерам",
    "🎯 Градация сделок",
    "⏱️ Время на этапах",
    "🤖 AI-аналитика"
])

# ===== TAB 1: OVERVIEW =====
with tab1:
    st.subheader("Суммарные показатели")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Сделок", len(view_df))
    c2.metric("Выручка, ₽", f"{int(view_df['Сумма'].sum()):,}")
    c3.metric("Ср. здоровье", f"{int(view_df['Здоровье'].mean())}%")
    c4.metric("Ср. потенциал", f"{int(view_df['Потенциал'].mean())}%")
    st.subheader("Распределение здоровья сделок")
    if px:
        fig1 = px.histogram(view_df, x="Здоровье", nbins=20, title="Распределение здоровья",
                            labels={"Здоровье":"Здоровье (%)"}, color_discrete_sequence=["#FF6B35"])
        st.plotly_chart(fig1, use_container_width=True)
    st.subheader("Топ-5 этапов по количеству сделок")
    if px:
        stage_counts = (view_df.groupby("Название этапа").size()
                        .reset_index(name="Количество")
                        .sort_values("Количество", ascending=False).head(5))
        fig2 = px.bar(stage_counts, x="Название этапа", y="Количество",
                      title="Топ-5 этапов", color="Количество", color_continuous_scale="Oranges")
        st.plotly_chart(fig2, use_container_width=True)
    st.subheader("Выручка по воронкам")
    if px:
        funnel_rev = view_df.groupby("Воронка")["Сумма"].sum().reset_index()
        fig3 = px.pie(funnel_rev, names="Воронка", values="Сумма", title="Распределение выручки")
        st.plotly_chart(fig3, use_container_width=True)

# ===== TAB 2: PROBLEMS =====
with tab2:
    st.subheader("Метрики проблем")
    no_tasks   = view_df[view_df["Нет задач"]]
    no_company = view_df[view_df["Нет компании"]]
    no_contact = view_df[view_df["Нет контакта"]]
    stuck      = view_df[view_df["Застряла"]]
    lost       = view_df[view_df["Проиграна"]]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Без задач", len(no_tasks), f"{len(no_tasks)/len(view_df)*100:.1f}%")
    c2.metric("Без компании", len(no_company), f"{len(no_company)/len(view_df)*100:.1f}%")
    c3.metric("Без контакта", len(no_contact), f"{len(no_contact)/len(view_df)*100:.1f}%")
    c4.metric("Застряли", len(stuck), f"{len(stuck)/len(view_df)*100:.1f}%")
    c5.metric("Проиграны", len(lost), f"{len(lost)/len(view_df)*100:.1f}%")
    if px:
        problem_counts = pd.DataFrame({
            "Проблема":["Без задач","Без компании","Без контакта","Застряли","Проиграны"],
            "Количество":[len(no_tasks),len(no_company),len(no_contact),len(stuck),len(lost)]
        })
        fig_prob = px.bar(problem_counts, x="Проблема", y="Количество",
                          title="Распределение проблем",
                          color="Количество", color_continuous_scale="Reds")
        st.plotly_chart(fig_prob, use_container_width=True)
    with st.expander(f"❗ Сделки без задач ({len(no_tasks)})"):
        if not no_tasks.empty:
            st.dataframe(no_tasks[["ID сделки","Название","Менеджер","Сумма","Здоровье"]],
                         use_container_width=True)
    with st.expander(f"🏢 Сделки без компании ({len(no_company)})"):
        if not no_company.empty:
            st.dataframe(no_company[["ID сделки","Название","Менеджер","Сумма","Здоровье"]],
                         use_container_width=True)
    with st.expander(f"📇 Сделки без контакта ({len(no_contact)})"):
        if not no_contact.empty:
            st.dataframe(no_contact[["ID сделки","Название","Менеджер","Сумма","Здоровье"]],
                         use_container_width=True)
    with st.expander(f"⏸️ Застрявшие сделки ({len(stuck)})"):
        if not stuck.empty:
            st.dataframe(stuck[["ID сделки","Название","Менеджер","Дней без активности","Здоровье"]],
                         use_container_width=True)
    with st.expander(f"❌ Проигранные сделки ({len(lost)})"):
        if not lost.empty:
            st.dataframe(lost[["ID сделки","Название","Менеджер","Сумма","Название этапа"]],
                         use_container_width=True)

# ===== TAB 3: BY MANAGER =====
with tab3:
    st.subheader("Аналитика по менеджерам")
    mgr_stats = []
    for mgr in selected_managers:
        mg = view_df[view_df["Менеджер"]==mgr]
        if mg.empty: continue
        total     = len(mg)
        revenue   = mg["Сумма"].sum()
        avg_health= mg["Здоровье"].mean()
        won       = len(mg[mg["Этап ID"].astype(str).str.contains("WON", case=False)])
        lost_cnt  = len(mg[mg["Проиграна"]])
        conv_rate = (won/total*100) if total>0 else 0
        base_quality=100 - (mg["Нет компании"].sum()+mg["Нет контакта"].sum())/(total*2)*100
        mgr_stats.append({
            "Менеджер":mgr,
            "Сделок":total,
            "Выручка, ₽":int(revenue),
            "Ср. здоровье, %":int(avg_health),
            "Конверсия в WON, %":round(conv_rate,1),
            "Качество базы, %":round(base_quality,1),
            "Выиграно":won,
            "Проиграно":lost_cnt
        })
    df_mgr = pd.DataFrame(mgr_stats)
    st.dataframe(df_mgr, use_container_width=True)
    if px and not df_mgr.empty:
        st.subheader("Визуализация по менеджерам")
        fig4 = px.bar(df_mgr, x="Менеджер", y="Выручка, ₽",
                      title="Выручка по менеджерам",
                      color="Ср. здоровье, %", color_continuous_scale="RdYlGn")
        st.plotly_chart(fig4, use_container_width=True)
        fig5 = px.scatter(df_mgr, x="Сделок", y="Конверсия в WON, %",
                          size="Выручка, ₽", hover_data=["Менеджер"],
                          title="Сделки vs Конверсия")
        st.plotly_chart(fig5, use_container_width=True)
    st.subheader("Конверсия по этапам воронки")
    conv_data = compute_conversion_by_manager_and_funnel(view_df, sort_map)
    for _, row in conv_data.iterrows():
        with st.expander(f"👤 {row['Менеджер']} | {row['Воронка']} ({row['Всего сделок']} сделок)"):
            stage_df = pd.DataFrame(row['Этапы'])
            st.dataframe(stage_df, use_container_width=True)
            if px and not stage_df.empty:
                fig6 = px.funnel(stage_df, x="Количество", y="Этап",
                                 title="Воронка конверсии")
                st.plotly_chart(fig6, use_container_width=True)

# ===== TAB 4: DEAL GRADATION =====
with tab4:
    st.subheader("Градация сделок по здоровью")
    quick = view_df[view_df["Быстрая победа?"]].sort_values("Скор быстрой победы", ascending=False)
    work  = view_df[(~view_df["Быстрая победа?"]) & (~view_df["Стоп-лист?"])].sort_values("Здоровье", ascending=False)
    drop  = view_df[view_df["Стоп-лист?"]].sort_values("Скор отказа", ascending=False)
    c1, c2, c3 = st.columns(3)
    c1.metric("🟢 Quick Wins", len(quick), f"{int(quick['Сумма'].sum()):,} ₽")
    c2.metric("🟡 Проработка", len(work), f"{int(work['Сумма'].sum()):,} ₽")
    c3.metric("🔴 Stop List", len(drop), f"{int(drop['Сумма'].sum()):,} ₽")
    if px:
        gradation_counts = pd.DataFrame({
            "Категория":["Quick Wins","Проработка","Stop List"],
            "Количество":[len(quick),len(work),len(drop)],
            "Сумма":[quick['Сумма'].sum(), work['Сумма'].sum(), drop['Сумма'].sum()]
        })
        fig7 = px.bar(gradation_counts, x="Категория", y="Количество",
                      title="Градация сделок",
                      color="Категория",
                      color_discrete_map={"Quick Wins":"green","Проработка":"orange","Stop List":"red"})
        st.plotly_chart(fig7, use_container_width=True)
    with st.expander(f"🟢 Quick Wins ({len(quick)})"):
        if not quick.empty:
            st.dataframe(quick[["ID сделки","Название","Менеджер","Сумма","Здоровье","Скор быстрой победы","ETA дней"]],
                         use_container_width=True)
    with st.expander(f"🟡 Проработка ({len(work)})"):
        if not work.empty:
            st.dataframe(work[["ID сделки","Название","Менеджер","Сумма","Здоровье","Потенциал"]],
                         use_container_width=True)
    with st.expander(f"🔴 Stop List ({len(drop)})"):
        if not drop.empty:
            st.dataframe(drop[["ID сделки","Название","Менеджер","Сумма","Здоровье","Скор отказа","Дней без активности"]],
                         use_container_width=True)

# ===== TAB 5: TIME ON STAGES =====
with tab5:
    st.subheader("Время на этапах воронки")
    stage_time = view_df.groupby("Название этапа").agg({
        "Дней на этапе":["mean","std","min","max"]
    }).round(1)
    stage_time.columns = ["Ср. дней","Откл. (σ)","Мин","Макс"]
    stage_time = stage_time.reset_index()
    st.dataframe(stage_time, use_container_width=True)
    if px and not stage_time.empty:
        fig8 = px.bar(stage_time, x="Название этапа", y="Ср. дней",
                      error_y="Откл. (σ)", title="Среднее время на этапах",
                      color="Ср. дней", color_continuous_scale="Blues")
        st.plotly_chart(fig8, use_container_width=True)
    st.subheader("Отклонения по сделкам")
    mean_stage_time = view_df.groupby("Этап ID")["Дней на этапе"].mean().to_dict()
    view_df["Отклонение дней"] = view_df.apply(
        lambda r: r["Дней на этапе"] - mean_stage_time.get(r["Этап ID"], 0), axis=1
    )
    outliers = view_df[abs(view_df["Отклонение дней"])>7].sort_values("Отклонение дней", ascending=False)
    st.dataframe(outliers[["ID сделки","Название","Менеджер","Название этапа","Дней на этапе","Отклонение дней"]],
                 use_container_width=True)

# ===== TAB 6: AI ANALYTICS =====
with tab6:
    st.subheader("🤖 AI-аналитика по менеджерам")
    for mgr in selected_managers:
        mg = view_df[view_df["Менеджер"]==mgr]
        if mg.empty: continue
        summary = {
            "total_deals": len(mg),
            "revenue": int(mg["Сумма"].sum()),
            "avg_health": int(mg["Здоровье"].mean()),
            "no_tasks": int(mg["Нет задач"].sum()),
            "no_company": int(mg["Нет компании"].sum()),
            "no_contact": int(mg["Нет контакта"].sum()),
            "stuck": int(mg["Застряла"].sum()),
            "lost": int(mg["Проиграна"].sum()),
            "won": len(mg[mg["Этап ID"].astype(str).str.contains("WON", case=False)])
        }
        with st.expander(f"👤 {mgr} ({len(mg)} сделок)"):
            with st.spinner("Генерирую AI-анализ..."):
                analysis = ai_analyze_manager(mgr, summary)
                st.markdown(analysis)

# ===== TAB 7: YEARLY PLAN =====
tab7 = st.tabs(["🎯 Годовой план"])[0]
with tab7:
    st.subheader("Годовой план по выручке")
    yearly_target = st.number_input("Цель на год, ₽", min_value=0, value=10_000_000, step=100_000, format="%d")
    start_month = st.selectbox("Стартовый месяц отчёта", list(range(1,13)), index=datetime.now().month-1)

    df_year = view_df.copy()
    df_year = df_year[df_year["Дата создания"].dt.year == datetime.now().year]
    df_year["Месяц"] = df_year["Дата создания"].dt.month

    # Фактическая выручка по месяцам
    actual = df_year.groupby("Месяц")["Сумма"].sum().reindex(range(1,13), fill_value=0)
    months = list(range(start_month, start_month+12))
    months = [((m-1)%12)+1 for m in months]

    # Оставшиеся месяцы
    current_month = datetime.now().month
    months_left = [m for m in months if m >= current_month]
    revenue_to_go = yearly_target - actual.sum()
    monthly_plan = {m: max(0, revenue_to_go / len(months_left)) for m in months_left}

    # Собираем DataFrame
    plan_df = pd.DataFrame({
        "Месяц": months,
        "Факт, ₽": [actual.get(m,0) for m in months],
        "План, ₽": [monthly_plan.get(m,0) if m in monthly_plan else None for m in months]
    })
    plan_df["План, ₽"] = plan_df["План, ₽"].ffill().bfill()

    # Визуализация
    if px:
        fig_plan = px.area(
            plan_df,
            x="Месяц",
            y=["Факт, ₽","План, ₽"],
            labels={"value":"Сумма, ₽","Месяц":"Месяц"},
            title="Факт vs План по месяцам",
            color_discrete_map={"Факт, ₽":"#2E91E5","План, ₽":"#E15F99"}
        )
        st.plotly_chart(fig_plan, use_container_width=True)

    st.subheader("Таблица плана и факта")
    st.dataframe(
        plan_df.assign(**{"Отклонение, ₽": plan_df["Факт, ₽"] - plan_df["План, ₽"]})
               .round(0),
        use_container_width=True
    )

st.markdown("---")
st.caption("БУРМАШ · CRM Дэшборд v4.2 | Powered by Bitrix24 + Perplexity AI")
