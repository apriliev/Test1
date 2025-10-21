# -*- coding: utf-8 -*-
"""
БУРМАШ · CRM Дэшборд v4.2
С AI-аналитикой, метриками проблем и аналитикой товаров
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

# ============ 1. CONFIG ============
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

# ============ 2. SECRETS ============
def get_secret(name, default=None):
    return st.secrets.get(name) or os.getenv(name, default) or default

BITRIX24_WEBHOOK   = (get_secret("BITRIX24_WEBHOOK","") or "").strip()
PERPLEXITY_API_KEY = (get_secret("PERPLEXITY_API_KEY","") or "").strip()

# ============ 3. BITRIX24 HELPERS ============
def _bx_get(method, params=None, pause=0.4):
    url = BITRIX24_WEBHOOK.rstrip("/") + f"/{method}.json"
    out, start = [], 0
    params = dict(params or {})
    while True:
        params["start"] = start
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        res = data.get("result")
        batch = (res.get("items",[]) if isinstance(res,dict) and "items" in res else res) or []
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
    if date_to:   params["filter[<=DATE_CREATE]"] = date_to
    deals = _bx_get("crm.deal.list", params)
    return deals[:limit]

@st.cache_data(ttl=300)
def bx_get_users_full():
    users = _bx_get("user.get", {})
    out = {}
    for u in users:
        depts = u.get("UF_DEPARTMENT") or []
        if isinstance(depts, str):
            depts = [int(x) for x in depts.split(",") if x]
        out[int(u["ID"])] = {
            "name": ((u.get("NAME","")+u.get("LAST_NAME","")).strip() or u.get("LOGIN","")).strip(),
            "depts": list(map(int,depts)) if depts else [],
            "active": (u.get("ACTIVE","Y")=="Y")
        }
    return out

@st.cache_data(ttl=300)
def bx_get_open_activities_for_deal_ids(deal_ids):
    out = {}
    if not deal_ids: return out
    for chunk in np.array_split(list(map(int,deal_ids)), max(1,len(deal_ids)//40+1)):
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
            sort_map[s["STATUS_ID"]] = int(s.get("SORT",5000))
            name_map[s["STATUS_ID"]] = s.get("NAME") or s["STATUS_ID"]
    except: pass
    for cid in cats:
        try:
            resp = _bx_get("crm.status.list", {"filter[ENTITY_ID]":f"DEAL_STAGE_{cid}"})
            for s in resp:
                sort_map[s["STATUS_ID"]] = int(s.get("SORT",5000))
                name_map[s["STATUS_ID"]] = s.get("NAME") or s["STATUS_ID"]
        except: continue
    return sort_map, name_map

@st.cache_data(ttl=600)
def bx_get_categories():
    try:
        cats = _bx_get("crm.category.list", {})
        return {int(c["ID"]): c.get("NAME","Воронка") for c in cats}
    except:
        return {}

# ============ 4. AI ANALYSIS ============
def ai_analyze_manager(manager_name, deals_summary):
    if not PERPLEXITY_API_KEY:
        return "AI-ключ не настроен."
    prompt = f"""
Ты эксперт по продажам и CRM-аналитике. Проанализируй работу менеджера.

Менеджер: {manager_name}
Данные: {json.dumps(deals_summary, ensure_ascii=False)}

Дай краткий анализ...
"""
    data = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system","content":"Ты эксперт по CRM-аналитике."},
            {"role": "user","content":prompt}
        ],
        "max_tokens":800,
        "temperature":0.3
    }
    try:
        resp = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization":f"Bearer {PERPLEXITY_API_KEY}"},
            json=data, timeout=30
        )
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка AI-анализа: {str(e)}"

# ============ 5. UTILS ============
def to_dt(x):
    try:
        ts = pd.to_datetime(x, utc=True, errors="coerce")
        if pd.isna(ts): return pd.NaT
        return ts.tz_convert(None)
    except: return pd.NaT

def days_between(later, earlier):
    a,b = to_dt(later), to_dt(earlier)
    if pd.isna(a) or pd.isna(b): return None
    return max(0,int((a-b)/pd.Timedelta(days=1)))

# ============ 6. SCORING ============
def compute_health_scores(df, open_tasks_map, stuck_days=5):
    now = to_dt(pd.Timestamp.utcnow())
    rows=[]
    for _,r in df.iterrows():
        create_dt=to_dt(r.get("DATE_CREATE"))
        last = to_dt(r.get("LAST_ACTIVITY_TIME")) or to_dt(r.get("DATE_MODIFY")) or create_dt
        begin_dt=to_dt(r.get("BEGINDATE")) or create_dt
        d_work=days_between(now,create_dt) or 0
        d_noact=days_between(now,last) or 0
        d_in_stage=days_between(now,begin_dt) or 0
        has_task=len(open_tasks_map.get(int(r["ID"]),[]))>0
        flags={
            "no_company":int(r.get("COMPANY_ID") or 0)==0,
            "no_contact":int(r.get("CONTACT_ID") or 0)==0,
            "no_tasks":not has_task,
            "stuck":d_noact>=stuck_days,
            "lost":str(r.get("STAGE_ID","")).upper().find("LOSE")>=0
        }
        score=100
        if flags["no_company"]: score-=10
        if flags["no_contact"]: score-=10
        if flags["no_tasks"]: score-=25
        if flags["stuck"]: score-=25
        if flags["lost"]: score=min(score,15)
        opp=float(r.get("OPPORTUNITY") or 0.0)
        prob=float(r.get("PROBABILITY") or 0.0)
        potential=min(100,int((opp>0)*(30+min(70,math.log10(max(1,opp))/5*70))*(0.4+prob/100*0.6)))
        rows.append({
            "ID сделки":int(r["ID"]),"Название":r.get("TITLE",""),
            "Менеджер ID":int(r.get("ASSIGNED_BY_ID") or 0),
            "Этап ID":r.get("STAGE_ID",""),
            "Воронка ID":r.get("CATEGORY_ID"),"Сумма":opp,
            "Вероятность":prob,"Дата создания":create_dt,
            "Дата изменения":to_dt(r.get("DATE_MODIFY")),"Последняя активность":last,
            "Начало этапа":begin_dt,"Дней в работе":d_work,
            "Дней без активности":d_noact,"Дней на этапе":d_in_stage,
            "Здоровье":max(0,min(100,int(score))),
            "Потенциал":max(0,min(100,int(potential))),
            "Нет компании":flags["no_company"],
            "Нет контакта":flags["no_contact"],
            "Нет задач":flags["no_tasks"],
            "Застряла":flags["stuck"],
            "Проиграна":flags["lost"]
        })
    return pd.DataFrame(rows)

def focus_scores(df, horizon_days=14, min_prob=50):
    if df.empty:
        return df.assign(**{"Скор быстрой победы":0.0, "ETA дней":np.nan, "Скор отказа":0.0})
    eps=1e-9
    prob=df["Вероятность"].clip(0,100)/100
    health=df["Здоровье"].clip(0,100)/100
    opp=df["Сумма"].clip(lower=0)
    opp_norm=np.log1p(opp)/max(np.log1p(opp).max(),eps)
    smin,smax=float(df["Сортировка этапа"].min()),float(df["Сортировка этапа"].max())
    if smax-smin<eps:
        stage_closeness=pd.Series(0.5,index=df.index)
    else:
        stage_closeness=(df["Сортировка этапа"]-smin)/(smax-smin)
    stage_closeness=np.where(df["Этап ID"].astype(str).str.contains("LOSE",na=False),0,stage_closeness)
    recency=1-(df["Дней без активности"].clip(lower=0)/max(horizon_days,1)).clip(0,1)
    quick=0.35*prob+0.25*health+0.15*recency+0.15*stage_closeness+0.10*opp_norm
    quick_score=(quick*100).round(1)
    eta=(30*(1-stage_closeness)*(1-0.5*health-0.5*prob)).clip(lower=0)
    eta_days=eta.round(0)
    age_norm=(df["Дней в работе"]/max(df["Дней в работе"].max(),1)).clip(0,1)
    noact_norm=(df["Дней без активности"]/max(df["Дней без активности"].max(),1)).clip(0,1)
    drop=(1-prob)*0.4+(1-health)*0.3+noact_norm*0.2+age_norm*0.1
    drop_score=(drop*100).round(1)
    out=df.copy()
    out["Скор быстрой победы"]=quick_score
    out["ETA дней"]=eta_days
    out["Скор отказа"]=drop_score
    out["Быстрая победа?"]=(quick_score>=60)&(df["Вероятность"]>=min_prob)&(~df["Проиграна"])
    out["Стоп-лист?"]=(drop_score>=70)|(df["Проиграна"])|((df["Здоровье"]<40)&(df["Дней без активности"]>horizon_days))
    return out

def compute_conversion_by_manager_and_funnel(df, sort_map):
    results=[]
    for (mgr,cat),g in df.groupby(["Менеджер","Воронка"],dropna=False):
        stages_sorted=sorted(g["Этап ID"].unique(),key=lambda s:sort_map.get(str(s),9999))
        counts=g.groupby("Этап ID").size()
        total=len(g)
        stage_data=[]
        for s in stages_sorted:
            cnt=counts.get(s,0)
            conv=cnt/total*100 if total>0 else 0
            stage_data.append({"Этап":s,"Количество":cnt,"Конверсия %":round(conv,1)})
        results.append({"Менеджер":mgr,"Воронка":cat,"Всего сделок":total,"Этапы":stage_data})
    return pd.DataFrame(results)

# ============ 7. SIDEBAR ============
st.sidebar.title("Фильтры")
date_from=st.sidebar.date_input("С даты",datetime.now().date()-timedelta(days=30))
date_to=st.sidebar.date_input("По дату",datetime.now().date())
stuck_days=st.sidebar.slider("Нет активности ≥ (дней)",2,21,5)
limit=st.sidebar.slider("Лимит сделок (API)",50,3000,600,step=50)
st.sidebar.title("Настройки фокуса РОПа")
focus_horizon=st.sidebar.slider("Горизонт фокуса (дней)",7,45,14)
focus_min_prob=st.sidebar.slider("Мин. вероятность для фокуса, %",0,100,50)

# ============ 8. LOAD DATA ============
with st.spinner("Загружаю данные…"):
    if not BITRIX24_WEBHOOK:
        st.error("Настройте BITRIX24_WEBHOOK в Secrets")
        st.stop()
    deals_raw=bx_get_deals(str(date_from),str(date_to),limit=limit)
    if not deals_raw:
        st.error("Сделок не найдено.")
        st.stop()
    df_raw=pd.DataFrame(deals_raw)
    df_raw["OPPORTUNITY"]=pd.to_numeric(df_raw.get("OPPORTUNITY"),errors="coerce").fillna(0.0)
    users_full=bx_get_users_full()
    users_map={uid:users_full[uid]["name"] for uid in users_full}
    open_tasks_map=bx_get_open_activities_for_deal_ids(df_raw["ID"].tolist())
    categories_map=bx_get_categories()

df_scores=compute_health_scores(df_raw,open_tasks_map,stuck_days)
stage_ids=df_scores["Этап ID"].dropna().unique().tolist()
sort_map,name_map=bx_get_stage_map(stage_ids)
FALLBACK_ORDER=["NEW","NEW_LEAD","PREPARATION","PREPAYMENT_INVOICE","EXECUTING","FINAL_INVOICE","WON","LOSE"]
df_scores["Сортировка этапа"]=df_scores["Этап ID"].map(lambda s:sort_map.get(str(s),FALLBACK_ORDER.index(str(s).split(":")[-1])*100 if str(s).split(":")[-1] in FALLBACK_ORDER else 10000))
df_scores["Название этапа"]=df_scores["Этап ID"].map(lambda s:name_map.get(str(s),str(s)))
df_scores["Менеджер"]=df_scores["Менеджер ID"].map(users_map).fillna("Неизвестно")
df_scores["Воронка"]=df_scores["Воронка ID"].map(lambda x:categories_map.get(int(x or 0),"Основная"))
df_scores=focus_scores(df_scores,focus_horizon,focus_min_prob)

funnels=sorted(df_scores["Воронка"].unique())
selected_funnels=st.sidebar.multiselect("Воронки",funnels,default=funnels)
managers=sorted(df_scores["Менеджер"].unique())
selected_managers=st.sidebar.multiselect("Менеджеры",managers,default=managers)

view_df=df_scores[df_scores["Воронка"].isin(selected_funnels)&df_scores["Менеджер"].isin(selected_managers)]
if view_df.empty:
    st.warning("Нет данных по выбранным фильтрам")
    st.stop()

# ============ 9. HEADER ============
st.markdown("# 🟧 БУРМАШ · CRM Дэшборд v4.2")
st.markdown(f"**Период**: {date_from} → {date_to} | **Сделок**: {len(view_df)}")

# ============ 10. MAIN TABS ============
tab1,tab2,tab3,tab4,tab5,tab6=st.tabs(["📊 Обзор","⚠️ Проблемы","👤 По менеджерам","🎯 Градация сделок","⏱️ Время на этапах","🤖 AI-аналитика"])

# -- TAB1 --
with tab1:
    c1,c2,c3,c4=st.columns(4)
    c1.metric("Сделок",len(view_df),key="m1")
    c2.metric("Выручка, ₽",f"{int(view_df['Сумма'].sum()):,}",key="m2")
    c3.metric("Ср. здоровье",f"{int(view_df['Здоровье'].mean())}%",key="m3")
    c4.metric("Ср. потенциал",f"{int(view_df['Потенциал'].mean())}%",key="m4")
    if px:
        fig1=px.histogram(view_df,x="Здоровье",nbins=20,title="Распределение здоровья",labels={"Здоровье":"Здоровье (%)"},color_discrete_sequence=["#FF6B35"])
        st.plotly_chart(fig1,use_container_width=True,key="fig1")
    # … аналогично для fig2(fig_key="fig2"), fig3("fig3")

# -- TAB2 --
with tab2:
    # … метрики проблем без изменений
    if px:
        problem_counts=pd.DataFrame({...})
        fig_prob=px.bar(problem_counts,...) 
        st.plotly_chart(fig_prob,use_container_width=True,key="fig_prob")

# -- TAB3 --
with tab3:
    # … датафрейм менеджеров
    if px and not df_mgr.empty:
        fig4=px.bar(df_mgr,...) 
        st.plotly_chart(fig4,use_container_width=True,key="fig4")
        fig5=px.scatter(df_mgr,...)
        st.plotly_chart(fig5,use_container_width=True,key="fig5")
    conv_data=compute_conversion_by_manager_and_funnel(view_df,sort_map)
    for i,row in conv_data.iterrows():
        with st.expander(f"👤 {row['Менеджер']} | {row['Воронка']} ({row['Всего сделок']})",key=f"exp_m{i}"):
            stage_df=pd.DataFrame(row["Этапы"])
            st.dataframe(stage_df,use_container_width=True)
            if px and not stage_df.empty:
                fig6=px.funnel(stage_df,x="Количество",y="Этап",title="Воронка конверсии")
                st.plotly_chart(fig6,use_container_width=True,key=f"fig6_{i}")

# -- TAB4, TAB5, TAB6 аналогично: всем plotly_chart и expander добавить уникальные key --

# ===== TAB 7: ТОВАРЫ =====
tab_products=st.tabs(["📦 Товары"])[0]
with tab_products:
    st.subheader("Аналитика товаров в сделках")
    @st.cache_data(ttl=300)
    def bx_get_deal_products(deal_id):
        try: return _bx_get("crm.deal.productrows.get",{"id":deal_id}) or []
        except: return []
    def load_all_products(view_df):
        recs=[]
        for deal_id in view_df["ID сделки"].unique():
            for p in bx_get_deal_products(deal_id):
                recs.append({...})
        return pd.DataFrame(recs)
    df_products=load_all_products(view_df)
    if df_products.empty:
        st.info("Нет товарных позиций")
    else:
        agg=(df_products.groupby("product_name"){"quantity":"sum","total":"sum"}
             .rename(...).reset_index())
        st.dataframe(agg,use_container_width=True)
        stock_raw=_bx_get("crm.catalog.store.product.list",{})
        df_stock=pd.DataFrame(stock_raw).rename(...).astype(...)
        last_30=datetime.now()-timedelta(days=30)
        recent_deals=df_raw[...] 
        recent_products=load_all_products(recent_deals)
        avg_daily=(recent_products.groupby("product_id")["quantity"].sum()/30).to_dict()
        recs2=[]
        for j,row in agg.iterrows():
            pid=...
            stock=...
            avg=avg_daily.get(pid,0)
            to_order=max(0,avg*14-stock)
            recs2.append({...})
        df_rec=pd.DataFrame(recs2).sort_values(... )
        st.subheader("Рекомендации по закупке")
        st.dataframe(df_rec,use_container_width=True,key="df_rec")

st.markdown("---")
st.caption("БУРМАШ · CRM Дэшборд v4.2 | Powered by Bitrix24 + Perplexity AI")
