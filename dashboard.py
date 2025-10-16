import streamlit as st
from analyzer import SimpleAnalyzer
import pandas as pd
import plotly.express as px

st.set_page_config(page_title="CRM-дэшборд | Bitrix24 + Perplexity (sonar-pro)", page_icon="🤖", layout="wide")
st.title("🤖 Аналитика CRM | Bitrix24 + Perplexity (sonar-pro)")

@st.cache_resource
def load_analyzer():
    return SimpleAnalyzer()

analyzer = load_analyzer()

st.sidebar.title("Фильтры данных")
date_from = st.sidebar.date_input("C какой даты?", pd.Timestamp.today() - pd.Timedelta(days=30))
date_to = st.sidebar.date_input("По какую дату?", pd.Timestamp.today())
run_button = st.sidebar.button("Обновить данные")

with st.spinner("Загружаю сделки с Bitrix24..."):
    deals = analyzer.get_deals(
        date_from=str(date_from) if date_from else None,
        date_to=str(date_to) if date_to else None,
        limit=50
    )
if not deals:
    st.warning("В Битрикс24 нет ни одной сделки за выбранный период. Проверьте фильтр.")
    st.stop()

df = pd.DataFrame(deals)
df['OPPORTUNITY'] = pd.to_numeric(df['OPPORTUNITY'], errors='coerce').fillna(0)
df['DATE_CREATE'] = pd.to_datetime(df['DATE_CREATE'], errors='coerce')
df['DATE_MODIFY'] = pd.to_datetime(df['DATE_MODIFY'], errors='coerce')

col1, col2, col3, col4 = st.columns(4)
col1.metric("Всего сделок", len(df))
col2.metric("Общий объём, ₽", int(df['OPPORTUNITY'].sum()))
col3.metric("Средний чек, ₽", int(df['OPPORTUNITY'].mean()))
col4.metric("Последнее обновление", str(df['DATE_MODIFY'].max())[:19])

st.subheader("Распределение по этапам сделки")
fig = px.bar(
    df.groupby('STAGE_ID').agg({'OPPORTUNITY': 'sum', 'ID': 'count'}).reset_index(),
    x='STAGE_ID', y='OPPORTUNITY',
    text='ID', labels={'STAGE_ID': "Этап", "OPPORTUNITY": "Сумма, ₽", "ID": "Сделок"},
    title="Суммы сделок по этапам"
)
st.plotly_chart(fig, use_container_width=True)

st.subheader("Таблица сделок")
st.dataframe(df[['ID', 'TITLE', 'STAGE_ID', 'OPPORTUNITY', 'ASSIGNED_BY_ID', 'DATE_CREATE', 'DATE_MODIFY']]
             .sort_values("DATE_CREATE", ascending=False), height=300)

st.subheader("🤖 Персональный анализ от Perplexity (sonar-pro)")
if st.button("Запустить AI-анализ"):
    with st.spinner("AI анализирует ваши сделки..."):
        result = analyzer.run_ai_analysis(deals)
        if isinstance(result, dict):
            st.success(f"Оценка здоровья воронки: **{result.get('health_score', 'N/A')}%**")
            st.info(result.get("summary", ""))
            st.markdown("**Рекомендации:**")
            for rec in result.get("recommendations", []):
                st.write("- " + rec)
        else:
            st.error("Ошибка обработки AI-анализа.")
else:
    st.info("Нажмите «Запустить AI-анализ», чтобы получить индивидуальные рекомендации.")

st.caption("© 2025 Битрикс24 + Perplexity PRO. sonar-pro аналитика")