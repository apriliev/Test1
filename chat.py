import streamlit as st
from analyzer import SimpleAnalyzer
import pandas as pd
import json

st.set_page_config(page_title="Чат CRM + Perplexity (sonar-pro)", page_icon="💬", layout="wide")
st.title("💬 Chat-бот Bitrix24 x Perplexity (sonar-pro)")

@st.cache_resource
def get_analyzer():
    return SimpleAnalyzer()

analyzer = get_analyzer()

st.sidebar.title("Данные из CRM")
date_from = st.sidebar.date_input("C какой даты?", pd.Timestamp.today() - pd.Timedelta(days=30))
date_to = st.sidebar.date_input("По какую дату?", pd.Timestamp.today())

deals = analyzer.get_deals(
    date_from=str(date_from) if date_from else None,
    date_to=str(date_to) if date_to else None,
    limit=50
)
if not deals:
    st.error("Нет ни одной сделки для анализа.")
    st.stop()

df = pd.DataFrame(deals)
summary = {
    "Всего сделок": len(df),
    "Общий объём (₽)": int(df['OPPORTUNITY'].astype(float).sum()),
    "По этапам": df.groupby('STAGE_ID').size().to_dict()
}

st.markdown(f"**Выгружено {summary['Всего сделок']} сделок на сумму {summary['Общий объём (₽)']} ₽**")
st.write("**По этапам:**", summary["По этапам"])

st.subheader("Спросите, что хотите о своих сделках 👇")
user_question = st.text_area("Ваш вопрос о CRM и продажах:",
                             placeholder="Например: 'Какие узкие места у нас?', 'Что улучшить?' и т.д.")

if st.button("Получить ответ от Perplexity"):
    prompt = f"""
Вот краткая CRM-сводка:

{json.dumps(summary, ensure_ascii=False, indent=2)}

Вопрос пользователя:

{user_question}

Ответь максимально полезно, лаконично, для бизнеса. Советы вынеси отдельным списком!
    """
    with st.spinner("Perplexity думает..."):
        import requests
        import os
        from dotenv import load_dotenv
        load_dotenv()
        url = "https://api.perplexity.ai/chat/completions"
        headers = {
            "Authorization": "Bearer " + os.getenv("PERPLEXITY_API_KEY"),
            "Content-Type": "application/json"
        }
        data = {
            "model": "sonar-pro",
            "messages": [
                {"role": "system", "content": "Отвечай на русском, по делу."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 900,
            "temperature": 0.19
        }
        resp = requests.post(url, headers=headers, json=data)
        if resp.ok:
            answer = resp.json()["choices"][0]["message"]["content"]
            st.markdown("**Ответ Perplexity:**")
            st.write(answer)
        else:
            st.error("Ошибка API или лимиты. Проверьте ключ и повторите.")

st.caption("© 2025 Bitrix24 + Perplexity PRO | Chat sonar-pro")