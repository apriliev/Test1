import os, requests, json, time, streamlit as st, pandas as pd, plotly.express as px, hashlib

# ===== АВТОРИЗАЦИЯ =====
def check_password():
    def password_entered():
        if (st.session_state.get("username")=="admin" and
            hashlib.sha256(st.session_state.get("password","").encode()).hexdigest()
            == "240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9"):
            st.session_state.password_correct = True
            del st.session_state.password
        else:
            st.session_state.password_correct = False

    if "password_correct" not in st.session_state:
        st.text_input("Логин", key="username")
        st.text_input("Пароль", type="password", key="password", on_change=password_entered)
        return False
    if not st.session_state.password_correct:
        st.error("Неверный логин или пароль")
        return False
    return True

if not check_password(): st.stop()
if st.sidebar.button("Выйти"):
    st.session_state.password_correct = False
    st.experimental_rerun()

# ===== ПАРАМЕТРЫ API =====
WEBHOOK = os.getenv("BITRIX24_WEBHOOK")
API_KEY = os.getenv("PERPLEXITY_API_KEY")
if not WEBHOOK or not API_KEY:
    st.error("Задайте BITRIX24_WEBHOOK и PERPLEXITY_API_KEY в Secrets")
    st.stop()

# ===== ФУНКЦИИ =====
def get_deals(frm, to):
    params = {
        "select[]": ["ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID", "DATE_CREATE"],
        "filter[>=DATE_CREATE]": frm,
        "filter[<=DATE_CREATE]": to
    }
    res = requests.get(WEBHOOK.rstrip("/") + "/crm.deal.list.json", params=params).json()
    return res.get("result", [])

def ai_analyze_deals(deals):
    prompt = (
        f"Ты эксперт по CRM. Сделок: {len(deals)}. "
        f"Примеры: {json.dumps(deals[:5], ensure_ascii=False)}. "
        "Верни JSON с полями "
        "health_score (int), pred_close_prob (float), summary (string), recommendations (list)."
    )
    data = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": "Строго JSON"},
            {"role": "user",   "content": prompt}
        ],
        "max_tokens": 1000,
        "temperature": 0.1
    }
    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=data
    ).json()
    text = resp["choices"][0]["message"]["content"]
    j = text[text.find("{"): text.rfind("}")+1]
    return json.loads(j)

# ===== UI =====
st.set_page_config(page_title="CRM AI Dashboard", layout="wide")
st.title("🤖 Аналитика CRM | Bitrix24 + Perplexity")

# --- ФИЛЬТРЫ ---
frm = st.sidebar.date_input("С какой даты", pd.Timestamp.today() - pd.Timedelta(days=30))
to  = st.sidebar.date_input("По какую дату", pd.Timestamp.today())
if st.sidebar.button("Загрузить сделки"):
    deals = get_deals(str(frm), str(to))
    df = pd.DataFrame(deals)

    # Список уникальных менеджеров
    df["ASSIGNED_BY_ID"] = df["ASSIGNED_BY_ID"].fillna("Unknown")
    managers = df["ASSIGNED_BY_ID"].unique().tolist()
    selected = st.sidebar.selectbox("Менеджер", ["Все"] + managers)

    # Фильтрация по менеджеру
    if selected != "Все":
        df = df[df["ASSIGNED_BY_ID"] == selected]

    st.subheader("Сделки")
    st.dataframe(df)

    if not df.empty:
        stats = ai_analyze_deals(df.to_dict("records"))
        st.subheader("Результаты AI-анализа")
        st.metric("Health score", f"{stats['health_score']}%")
        st.metric("Prob. to close", f"{stats['pred_close_prob']*100:.1f}%")
        st.markdown("**Summary:**")
        st.write(stats["summary"])
        st.markdown("**Recommendations:**")
        for rec in stats["recommendations"]:
            st.write(f"- {rec}")

        # График
        fig = px.bar(
            x=["health_score", "pred_close_prob"],
            y=[stats["health_score"], stats["pred_close_prob"]],
            labels={"x": "Метрика", "y": "Значение"}
        )
        st.subheader("График метрик")
        st.plotly_chart(fig, use_container_width=True)
