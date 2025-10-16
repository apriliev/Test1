import os
import requests
import json
import time
import streamlit as st
import pandas as pd
import plotly.express as px
import hashlib

# ===== ПРОСТАЯ АВТОРИЗАЦИЯ =====
def check_password():
    def password_entered():
        if (st.session_state["username"] == "admin" and
            hashlib.sha256(st.session_state["password"].encode()).hexdigest()
            == "240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9"):
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.markdown("### 🔐 Вход в систему")
        st.text_input("Логин", key="username")
        st.text_input("Пароль", type="password", key="password", on_change=password_entered)
        return False
    elif not st.session_state["password_correct"]:
        st.markdown("### 🔐 Вход в систему")
        st.text_input("Логин", key="username")
        st.text_input("Пароль", type="password", key="password", on_change=password_entered)
        st.error("❌ Неверный логин или пароль")
        return False
    return True

if not check_password():
    st.stop()
if st.sidebar.button("Выйти"):
    st.session_state["password_correct"] = False
    st.experimental_rerun()

# ===== API НАСТРОЙКИ =====
BITRIX24_WEBHOOK = os.getenv("BITRIX24_WEBHOOK")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"

if not BITRIX24_WEBHOOK or not PERPLEXITY_API_KEY:
    st.error("❌ Задайте BITRIX24_WEBHOOK и PERPLEXITY_API_KEY в Secrets")
    st.stop()

# ===== ФУНКЦИИ =====
def get_deals(date_from=None, date_to=None, limit=50, pause_sec=1.0):
    deals, start = [], 0
    params = {"select[]": [
        "ID","TITLE","STAGE_ID","OPPORTUNITY","ASSIGNED_BY_ID",
        "DATE_CREATE","DATE_MODIFY","LAST_ACTIVITY_TIME","PROBABILITY"
    ]}
    if date_from: params["filter[>=DATE_CREATE]"] = date_from
    if date_to:   params["filter[<=DATE_CREATE]"] = date_to

    while True:
        params["start"] = start
        r = requests.get(BITRIX24_WEBHOOK.rstrip("/") + "/crm.deal.list.json", params=params)
        res = r.json()
        if not res.get("result"): break
        deals.extend(res["result"])
        if len(deals) >= limit or len(res["result"]) < 50: break
        start += 50
        time.sleep(pause_sec)
    return deals[:limit]

def run_ai_analysis(deals):
    if not deals:
        return {"health_score":0,"summary":"Нет данных","recommendations":["Добавьте сделки"]}
    sample = deals[:10]
    prompt = f"""
Ты эксперт по CRM. Сделок: {len(deals)}. Примеры: {json.dumps(sample, ensure_ascii=False, indent=2)}
Ответ в формате JSON с ключами health_score, summary, recommendations.
    """
    data = {
        "model":"sonar-pro",
        "messages":[
            {"role":"system","content":"Ты даёшь строго валидный JSON без текста оберток."},
            {"role":"user","content":prompt}
        ],
        "max_tokens":1000,
        "temperature":0.1
    }
    resp = requests.post(PERPLEXITY_API_URL,
                         headers={"Authorization":f"Bearer {PERPLEXITY_API_KEY}"},
                         json=data)
    text = resp.json().get("choices",[{}])[0].get("message",{}).get("content","")
    start,end = text.find("{"), text.rfind("}")+1
    try:
        return json.loads(text[start:end])
    except:
        return {"health_score":0,"summary":"Ошибка анализа","recommendations":[]}

# ===== UI =====
st.set_page_config(page_title="CRM-дэшборд", page_icon="🤖", layout="wide")
st.title("🤖 Аналитика CRM | Bitrix24 + Perplexity")
st.sidebar.success("✅ Вы вошли")
st.sidebar.title("Фильтры периода")
date_from = st.sidebar.date_input("С какой даты?", pd.Timestamp.today() - pd.Timedelta(days=30))
date_to   = st.sidebar.date_input("По какую дату?", pd.Timestamp.today())
if st.sidebar.button("Обновить"):
    pass

with st.spinner("Загрузка сделок..."):
    deals = get_deals(str(date_from), str(date_to), limit=50)

if not deals:
    st.warning("Нет сделок за этот период.")
    st.stop()

df = pd.DataFrame(deals)
df["OPPORTUNITY"] = pd.to_numeric(df["OPPORTUNITY"], errors="coerce").fillna(0)
df["DATE_CREATE"] = pd.to_datetime(df["DATE_CREATE"], errors="coerce")
df["DATE_MODIFY"] = pd.to_datetime(df["DATE_MODIFY"], errors="coerce")

c1,c2,c3,c4 = st.columns(4)
c1.metric("Сделок", len(df))
c2.metric("Объём, ₽", f"{int(df.OPPORTUNITY.sum()):,}")
c3.metric("Средний, ₽", f"{int(df.OPPORTUNITY.mean()):,}")
c4.metric("Обновлено", str(df.DATE_MODIFY.max())[:19])

st.subheader("📊 Этапы")
fig = px.bar(df.groupby("STAGE_ID").agg({"OPPORTUNITY":"sum","ID":"count"}).reset_index(),
             x="STAGE_ID", y="OPPORTUNITY", text="ID")
st.plotly_chart(fig, use_container_width=True)

st.subheader("📋 Таблица")
st.dataframe(
    df[["ID","TITLE","STAGE_ID","OPPORTUNITY","DATE_CREATE","DATE_MODIFY"]]
    .sort_values("DATE_CREATE", ascending=False),
    height=300
)

st.subheader("🤖 AI-анализ")
if st.button("Запустить анализ"):
    result = run_ai_analysis(deals)
    st.success(f"Оценка: {result.get('health_score','N/A')}%")
    st.info(result.get("summary",""))
    for item in result.get("recommendations",[]): st.write(f"- {item}")
