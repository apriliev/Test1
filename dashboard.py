# -*- coding: utf-8 -*-
"""
БУРМАШ · CRM Дэшборд v4.0
Расширенная аналитика в стиле RUBI Chat + предиктивный фокус РОПа
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
            "days_in_work": d_work,
            "days_no_activity": d_noact,
            "days_in_stage": d_in_stage,
            "health": max(0, min(100, int(score))),
            "potential": max(0, min(100, int(potential))),
            "flag_no_company": flags["no_company"],
            "flag_no_contact": flags["no_contact"],
            "flag_no_tasks": flags["no_tasks"],
            "flag_stuck": flags["stuck"],
            "flag_lost": flags["lost"],
        })
    return pd.DataFrame(rows)

def deal_recommendations(row):
    recs = []
    if row["flag_lost"]:
        return ["Проверьте причину проигрыша, при шансе — верните сделку в работу."]
    if row["flag_no_tasks"]:
        recs.append("Поставьте задачу на следующий шаг.")
    if row["flag_stuck"]:
        recs.append("Нет активности — звонок + письмо-резюме сегодня.")
    if row["flag_no_contact"]:
        recs.append("Добавьте контакт ЛПР.")
    if row["flag_no_company"]:
        recs.append("Заполните карточку компании.")
    if row["health"] < 60 and row["potential"] >= 50:
        recs.append("Высокий потенциал при низком здоровье — встреча/демо.")
    if not recs:
        recs.append("Продолжайте по этапу.")
    return recs

# ============ FOCUS SCORES ============
def focus_scores(df, horizon_days=14, min_prob=50):
    if df.empty:
        return df.assign(quick_score=0.0, eta_days=np.nan, drop_score=0.0)
    eps = 1e-9
    prob = df["PROBABILITY"].clip(0, 100) / 100.0
    health = df["health"].clip(0,100) / 100.0
    potential = df["potential"].clip(0,100) / 100.0
    opp = df["OPPORTUNITY"].clip(lower=0)
    opp_norm = np.log1p(opp) / max(np.log1p(opp).max(), eps)
    smin, smax = float(df["stage_sort"].min()), float(df["stage_sort"].max())
    if smax - smin < eps:
        stage_closeness = pd.Series(0.5, index=df.index)
    else:
        stage_closeness = (df["stage_sort"] - smin) / (smax - smin)
    stage_closeness = np.where(df["STAGE_ID"].astype(str).str.contains("LOSE", case=False, na=False), 0.0, stage_closeness)
    recency = 1 - (df["days_no_activity"].clip(lower=0) / max(horizon_days, 1)).clip(0,1)
    quick = ( 0.35*prob + 0.25*health + 0.15*recency + 0.15*stage_closeness + 0.10*opp_norm )
    quick_score = (quick*100).round(1)
    eta = (30*(1-stage_closeness)*(1 - 0.5*health - 0.5*prob)).clip(lower=0)
    eta_days = eta.round(0)
    age_norm = (df["days_in_work"]/max(df["days_in_work"].max(),1)).clip(0,1)
    noact_norm = (df["days_no_activity"]/max(df["days_no_activity"].max(),1)).clip(0,1)
    drop = (1-prob)*0.4 + (1-health)*0.3 + noact_norm*0.2 + age_norm*0.1
    drop_score = (drop*100).round(1)
    out = df.copy()
    out["quick_score"] = quick_score
    out["eta_days"] = eta_days
    out["drop_score"] = drop_score
    out["is_quick"] = (out["quick_score"]>=60) & (out["PROBABILITY"]>=min_prob) & (~out["flag_lost"])
    out["is_drop"] = (out["drop_score"]>=70) | (out["flag_lost"]) | ((out["health"]<40) & (out["days_no_activity"]>horizon_days))
    return out

# ============ CONVERSION FUNNEL ============
def compute_conversion_by_manager_and_funnel(df, sort_map):
    """
    Возвращает DataFrame с конверсией по этапам для каждого менеджера и воронки
    """
    results = []
    for (mgr, cat), g in df.groupby(["manager", "funnel"], dropna=False):
        stages_sorted = sorted(g["STAGE_ID"].unique(), key=lambda s: sort_map.get(str(s), 9999))
        stage_counts = g.groupby("STAGE_ID").size()
        total = len(g)
        stage_data = []
        for s in stages_sorted:
            cnt = stage_counts.get(s, 0)
            conv = (cnt / total * 100) if total > 0 else 0
            stage_data.append({"stage": s, "count": cnt, "conversion": conv})
        results.append({
            "manager": mgr,
            "funnel": cat,
            "total_deals": total,
            "stages": stage_data
        })
    return pd.DataFrame(results)

# ============ SIDEBAR ============
st.sidebar.title("Фильтры")
date_from = st.sidebar.date_input("С даты", datetime.now().date() - timedelta(days=30))
date_to = st.sidebar.date_input("По дату", datetime.now().date())
stuck_days = st.sidebar.slider("Нет активности ≥ (дней)", 2, 21, 5)
limit = st.sidebar.slider("Лимит сделок (API)", 50, 3000, 600, step=50)

st.sidebar.title("Настройки фокуса РОПа")
focus_horizon = st.sidebar.slider("Горизонт фокуса (дней)", 7, 45, 14)
focus_min_prob = st.sidebar.slider("Мин. вероятность для фокуса, %", 0, 100, 50)

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
    categories_map = bx_get_categories()

# Score
df_scores = compute_health_scores(df_raw, open_tasks_map, stuck_days=stuck_days)

# Map stages
stage_ids = df_scores["STAGE_ID"].dropna().unique().tolist()
sort_map, name_map = bx_get_stage_map(stage_ids)
FALLBACK_ORDER = ["NEW","NEW_LEAD","PREPARATION","PREPAYMENT_INVOICE","EXECUTING","FINAL_INVOICE","WON","LOSE"]
def fallback_sort(sid):
    sid = str(sid or "")
    sid_short = sid.split(":")[1] if ":" in sid else sid
    return (FALLBACK_ORDER.index(sid_short)*100 if sid_short in FALLBACK_ORDER else 10000 + hash(sid_short)%1000)

df_scores["stage_sort"] = df_scores["STAGE_ID"].map(lambda s: sort_map.get(str(s), fallback_sort(s)))
df_scores["stage_name"] = df_scores["STAGE_ID"].map(lambda s: name_map.get(str(s), str(s)))
df_scores["manager"] = df_scores["ASSIGNED_BY_ID"].map(users_map).fillna("Неизвестно")
df_scores["funnel"] = df_scores["CATEGORY_ID"].map(lambda x: categories_map.get(int(x or 0), "Основная"))

# Focus scores
df_scores = focus_scores(df_scores, horizon_days=focus_horizon, min_prob=focus_min_prob)

# ============ FILTERS ============
funnels = sorted(df_scores["funnel"].unique())
selected_funnels = st.sidebar.multiselect("Воронки", funnels, default=funnels)

managers = sorted(df_scores["manager"].unique())
selected_managers = st.sidebar.multiselect("Менеджеры", managers, default=managers)

view_df = df_scores[
    (df_scores["funnel"].isin(selected_funnels)) &
    (df_scores["manager"].isin(selected_managers))
].copy()

if view_df.empty:
    st.warning("Нет данных по выбранным фильтрам.")
    st.stop()

# ============ HEADER ============
st.markdown("# 🟧 БУРМАШ · CRM Дэшборд v4.0")
st.markdown(f"**Период**: {date_from} → {date_to} | **Сделок**: {len(view_df)}")

# ============ TABS ============
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 Обзор",
    "👤 По менеджерам",
    "🎯 Градация сделок",
    "⏱️ Время на этапах",
    "🚦 Западания и рекомендации"
])

# ===== TAB 1: OVERVIEW =====
with tab1:
    st.subheader("Суммарные показатели")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Сделок", len(view_df))
    c2.metric("Выручка, ₽", f"{int(view_df['OPPORTUNITY'].sum()):,}")
    c3.metric("Ср. здоровье", f"{int(view_df['health'].mean())}%")
    c4.metric("Ср. потенциал", f"{int(view_df['potential'].mean())}%")
    
    st.subheader("Распределение здоровья")
    if px:
        fig = px.histogram(view_df, x="health", nbins=20, title="Health Score")
        st.plotly_chart(fig, use_container_width=True)

# ===== TAB 2: BY MANAGER =====
with tab2:
    st.subheader("Аналитика по менеджерам")
    
    mgr_stats = []
    for mgr in selected_managers:
        mg = view_df[view_df["manager"]==mgr]
        if mg.empty: continue
        total = len(mg)
        revenue = mg["OPPORTUNITY"].sum()
        avg_health = mg["health"].mean()
        won = len(mg[mg["STAGE_ID"].astype(str).str.contains("WON", case=False, na=False)])
        lost = len(mg[mg["flag_lost"]])
        conv_rate = (won / total * 100) if total > 0 else 0
        base_quality = 100 - (mg["flag_no_company"].sum() + mg["flag_no_contact"].sum()) / (total * 2) * 100
        mgr_stats.append({
            "Менеджер": mgr,
            "Сделок": total,
            "Выручка, ₽": int(revenue),
            "Ср. здоровье, %": int(avg_health),
            "Конверсия в WON, %": round(conv_rate, 1),
            "Качество базы, %": round(base_quality, 1),
            "Выиграно": won,
            "Проиграно": lost
        })
    
    df_mgr = pd.DataFrame(mgr_stats)
    st.dataframe(df_mgr, use_container_width=True)
    
    st.subheader("Конверсия по этапам воронки")
    conv_data = compute_conversion_by_manager_and_funnel(view_df, sort_map)
    for _, row in conv_data.iterrows():
        with st.expander(f"👤 {row['manager']} | {row['funnel']} ({row['total_deals']} сделок)"):
            stage_df = pd.DataFrame(row['stages'])
            st.dataframe(stage_df, use_container_width=True)

# ===== TAB 3: DEAL GRADATION =====
with tab3:
    st.subheader("Градация сделок по здоровью")
    
    quick = view_df[view_df["is_quick"]].sort_values("quick_score", ascending=False)
    work = view_df[(~view_df["is_quick"]) & (~view_df["is_drop"])].sort_values("health", ascending=False)
    drop = view_df[view_df["is_drop"]].sort_values("drop_score", ascending=False)
    
    c1, c2, c3 = st.columns(3)
    c1.metric("🟢 Quick Wins", len(quick))
    c2.metric("🟡 Проработка", len(work))
    c3.metric("🔴 Stop List", len(drop))
    
    with st.expander(f"🟢 Quick Wins ({len(quick)})"):
        if not quick.empty:
            st.dataframe(quick[["ID","TITLE","manager","OPPORTUNITY","health","quick_score","eta_days"]], use_container_width=True)
    
    with st.expander(f"🟡 Проработка ({len(work)})"):
        if not work.empty:
            st.dataframe(work[["ID","TITLE","manager","OPPORTUNITY","health","potential"]], use_container_width=True)
    
    with st.expander(f"🔴 Stop List ({len(drop)})"):
        if not drop.empty:
            st.dataframe(drop[["ID","TITLE","manager","OPPORTUNITY","health","drop_score","days_no_activity"]], use_container_width=True)

# ===== TAB 4: TIME ON STAGES =====
with tab4:
    st.subheader("Время на этапах воронки")
    
    stage_time = view_df.groupby("STAGE_ID").agg({
        "days_in_stage": ["mean", "std", "min", "max"]
    }).round(1)
    stage_time.columns = ["Ср. дней", "Откл. (σ)", "Мин", "Макс"]
    stage_time["Этап"] = stage_time.index.map(lambda s: name_map.get(str(s), str(s)))
    stage_time = stage_time.reset_index(drop=True)
    st.dataframe(stage_time, use_container_width=True)
    
    st.subheader("Отклонения по сделкам")
    mean_stage_time = view_df.groupby("STAGE_ID")["days_in_stage"].mean().to_dict()
    view_df["deviation_days"] = view_df.apply(
        lambda r: r["days_in_stage"] - mean_stage_time.get(r["STAGE_ID"], 0), axis=1
    )
    outliers = view_df[abs(view_df["deviation_days"]) > 7].sort_values("deviation_days", ascending=False)
    st.dataframe(outliers[["ID","TITLE","manager","stage_name","days_in_stage","deviation_days"]], use_container_width=True)

# ===== TAB 5: BOTTLENECKS & RECOMMENDATIONS =====
with tab5:
    st.subheader("Западания и рекомендации по менеджерам")
    
    for mgr in selected_managers:
        mg = view_df[view_df["manager"]==mgr]
        if mg.empty: continue
        
        bottlenecks = []
        if (mg["flag_no_tasks"].sum() / len(mg)) > 0.3:
            bottlenecks.append(f"❗ {int(mg['flag_no_tasks'].sum())} сделок без задач (>{30}%)")
        if (mg["flag_stuck"].sum() / len(mg)) > 0.2:
            bottlenecks.append(f"⏸️ {int(mg['flag_stuck'].sum())} застрявших сделок (>{20}%)")
        if mg["health"].mean() < 60:
            bottlenecks.append(f"⚠️ Среднее здоровье {int(mg['health'].mean())}% < 60%")
        if (mg["flag_no_contact"].sum() / len(mg)) > 0.15:
            bottlenecks.append(f"📇 {int(mg['flag_no_contact'].sum())} сделок без контакта")
        
        with st.expander(f"👤 {mgr} ({len(mg)} сделок)"):
            if bottlenecks:
                st.markdown("**Западания:**")
                for b in bottlenecks:
                    st.markdown(f"- {b}")
                st.markdown("**Рекомендации:**")
                st.markdown("- Провести разбор «зависших» сделок: причины, барьеры, план действий")
                st.markdown("- Поставить задачи на каждую сделку без активности")
                st.markdown("- Заполнить контакты ЛПР для повышения качества базы")
                st.markdown("- Назначить встречи/демо по сделкам с высоким потенциалом")
            else:
                st.success("✅ Нет критичных западаний. Продолжайте работу по плану!")

st.markdown("---")
st.caption("БУРМАШ · CRM Дэшборд v4.0 | Powered by Bitrix24 + Perplexity AI")
