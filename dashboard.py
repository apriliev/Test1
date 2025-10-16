import streamlit as st
from analyzer import SimpleAnalyzer
import pandas as pd
import plotly.express as px
import hashlib

st.set_page_config(page_title="CRM-дэшборд | Bitrix24 + Perplexity", page_icon="🤖", layout="wide")

# ===== ПРОСТАЯ АВТОРИЗАЦИЯ =====
def check_password():
    """Возвращает True если пароль правильный"""
    def password_entered():
        """Проверяет пароль"""
        if (st.session_state["username"] == "admin" and 
            hashlib.sha256(st.session_state["password"].encode()).hexdigest() == 
            "240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9"):  # admin123
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # Удаляем пароль из памяти
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        # Первый запуск - показываем форму входа
        st.markdown("### 🔐 Вход в систему")
        st.text_input("Логин", key="username")
        st.text_input("Пароль", type="password", key="password", on_change=password_entered)
        return False
    elif not st.session_state["password_correct"]:
        # Неправильный пароль
        st.markdown("### 🔐 Вход в систему")
        st.text_input("Логин", key="username")
        st.text_input("Пароль", type="password", key="password", on_change=password_entered)
        st.error("❌ Неверный логин или пароль")
        return False
    else:
        # Пароль правильный
        return True

if not check_password():
    st.stop()

# ===== ГЛАВНЫЙ ДЭШБОРД =====
st.title("🤖 Аналитика CRM | Bitrix24 + Perplexity")

# Кнопка выхода
if st.sidebar.button("Выйти"):
    st.session_state["password_correct"] = False
    st.rerun()

st.sidebar.success("✅ Вы вошли в систему")

@st.cache_resource
def load_analyzer():
    return SimpleAnalyzer()

analyzer = load_analyzer()

st.sidebar.title("Фильтры данных")
date_from = st.sidebar.date_input("C какой даты?", pd.Timestamp.today() - pd.Timedelta(days=30))
date_to = st.sidebar.date_input("По какую дату?", pd.Timestamp.today())

with st.spinner("Загружаю сделки с Bitrix24..."):
    deals = analyzer.get_deals(
        date_from=str(date_from),
        date_to=str(date_to),
        limit=50
    )

if not deals:
    st.warning("В Битрикс24 нет ни одной сделки за выбранный период.")
    st.stop()

df = pd.DataFrame(deals)
df['OPPORTUNITY'] = pd.to_numeric(df['OPPORTUNITY'], errors='coerce').fillna(0)
df['DATE_CREATE'] = pd.to_datetime(df['DATE_CREATE'], errors='coerce')
df['DATE_MODIFY'] = pd.to_datetime(df['DATE_MODIFY'], errors='coerce')

col1, col2, col3, col4 = st.columns(4)
col1.metric("Всего сделок", len(df))
col2.metric("Общий объём, ₽", f"{int(df['OPPORTUNITY'].sum()):,}")
col3.metric("Средний чек, ₽", f"{int(df['OPPORTUNITY'].mean()):,}")
col4.metric("Обновлено", str(df['DATE_MODIFY'].max())[:19])

st.subheader("📊 Распределение по этапам")
fig = px.bar(
    df.groupby('STAGE_ID').agg({'OPPORTUNITY': 'sum', 'ID': 'count'}).reset_index(),
    x='STAGE_ID', y='OPPORTUNITY', text='ID',
    labels={'STAGE_ID': "Этап", "OPPORTUNITY": "Сумма, ₽", "ID": "Сделок"}
)
st.plotly_chart(fig, use_container_width=True)

st.subheader("📋 Таблица сделок")
st.dataframe(
    df[['ID', 'TITLE', 'STAGE_ID', 'OPPORTUNITY', 'DATE_CREATE', 'DATE_MODIFY']]
    .sort_values("DATE_CREATE", ascending=False),
    height=300
)

st.subheader("🤖 AI-анализ от Perplexity")
if st.button("🚀 Запустить анализ"):
    with st.spinner("AI думает..."):
        result = analyzer.run_ai_analysis(deals)
        st.success(f"💯 Оценка: **{result.get('health_score', 'N/A')}%**")
        st.info(result.get("summary", ""))
        st.markdown("**📝 Рекомендации:**")
        for rec in result.get("recommendations", []):
            st.write(f"✅ {rec}")

st.caption("© 2025 Битрикс24 + Perplexity PRO")
