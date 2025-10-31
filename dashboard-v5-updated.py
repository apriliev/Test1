# -*- coding: utf-8 -*-
"""
БУРМАШ · CRM Дэшборд v5.0
Обновленная версия с новыми требованиями:
- Новые фильтры (НИТ, год, квартал, месяц, неделя)
- Динамика по сравнению с предыдущим периодом
- Правильный расчет выручки из 3 воронок
- Анализ провалов по группам стадий
- Расширенный годовой план с прогнозом
- AI-рекомендации по здоровью и отслеживание обхода системы
"""

import os, time, math, json
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
import numpy as np
import pandas as pd
import streamlit as st
import requests

try:
    import plotly.express as px
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except:
    px = go = None

# ============ CONFIG ============
st.set_page_config(page_title="БУРМАШ · CRM v5.0", page_icon="🟧", layout="wide")

# ============ АВТОРИЗАЦИЯ ============
AUTH_KEY = "burmash_auth_ok"

def require_auth():
    if AUTH_KEY not in st.session_state:
        st.session_state[AUTH_KEY] = False
    
    if st.session_state[AUTH_KEY]:
        return
    
    st.markdown("### 🔐 Вход — БУРМАШ")
    with st.form("login_form", clear_on_submit=False):
        login = st.text_input("Логин", value="", key="auth_user")
        password = st.text_input("Пароль", value="", type="password", key="auth_pass")
        ok = st.form_submit_button("Войти")
        
        if ok:
            st.session_state[AUTH_KEY] = (login == "admin" and password == "admin123")
            if not st.session_state[AUTH_KEY]:
                st.error("Неверный логин или пароль")
            st.rerun()
    st.stop()

require_auth()

with st.sidebar:
    if st.button("Выйти", key="logout_btn"):
        st.session_state[AUTH_KEY] = False
        st.rerun()

def get_secret(name, default=None):
    return st.secrets.get(name) or os.getenv(name, default) or default

BITRIX24_WEBHOOK = (get_secret("BITRIX24_WEBHOOK", "") or "").strip()
PERPLEXITY_API_KEY = (get_secret("PERPLEXITY_API_KEY", "") or "").strip()

# ============ КОНСТАНТЫ ============
# Стадии провала для воронок
FAILURE_STAGES_GROUP1 = {
    "main_stages": ["Неразобранное", "В работе", "Сделка квалифицирована", "Квалифицирована как не приоритетная"],
    "failure_stages": ["Недозвон", "Не абонент", "СПАМ", "Нецелевой", "Дорого", 
                      "Организация не действует", "Был конфликт", "Не одобрили отсрочку платежа",
                      "Не устроили сроки", "Сделка отменена клиентом", "Удалено из неразобр. Авито"]
}

FAILURE_STAGES_GROUP2 = {
    "main_stages": ["КП отправлено", "Счёт выставлен/Документы подготовлены"],
    "failure_stages": ["Выбрали конкурентов", "Дорого", "Был конфликт",
                      "Не одобрили отсрочку платежа", "Не устроили сроки", "Сделка отменена клиентом"]
}

# Стадии успешного закрытия для разных воронок
SUCCESS_STAGES = {
    "Основная воронка продаж": "Успешно реализовано",
    "Физ.Лица": "Сделка успешна",
    "Не приоритетные сделки": "Сделка успешна"
}

# ============ BITRIX HELPERS ============
def _bx_get(method, params=None, pause=0.4):
    """Базовый метод для получения данных из Bitrix24"""
    url = BITRIX24_WEBHOOK.rstrip("/") + f"/{method}.json"
    out, start = [], 0
    params = dict(params or {})
    
    while True:
        params["start"] = start
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        res = data.get("result")
        batch = (res.get("items", []) if isinstance(res, dict) and "items" in res else res) or []
        
        if not batch:
            break
        
        out.extend(batch)
        if len(batch) < 50:
            break
        
        start += 50
        time.sleep(pause)
    
    return out

@st.cache_data(ttl=300)
def bx_get_deals(date_from=None, date_to=None, limit=5000):
    """Получение сделок с расширенными полями"""
    params = {"select[]": [
        "ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "ASSIGNED_BY_ID", 
        "COMPANY_ID", "CONTACT_ID", "PROBABILITY", "DATE_CREATE", 
        "DATE_MODIFY", "LAST_ACTIVITY_TIME", "CATEGORY_ID", 
        "BEGINDATE", "CLOSEDATE", "CLOSED"
    ]}
    
    if date_from:
        params["filter[>=DATE_CREATE]"] = date_from
    if date_to:
        params["filter[<=DATE_CREATE]"] = date_to
    
    deals = _bx_get("crm.deal.list", params)
    return deals[:limit]

@st.cache_data(ttl=300)
def bx_get_users_full():
    """Получение пользователей"""
    users = _bx_get("user.get", {})
    out = {}
    
    for u in users:
        depts = u.get("UF_DEPARTMENT") or []
        if isinstance(depts, str):
            depts = [int(x) for x in depts.split(",") if x]
        
        out[int(u["ID"])] = {
            "name": ((u.get("NAME", "") + " " + u.get("LAST_NAME", "")).strip() or u.get("LOGIN", "")).strip(),
            "depts": list(map(int, depts)) if depts else [],
            "active": (u.get("ACTIVE", "Y") == "Y")
        }
    
    return out

@st.cache_data(ttl=300)
def bx_get_open_activities_for_deal_ids(deal_ids):
    """Получение открытых активностей для сделок"""
    out = {}
    if not deal_ids:
        return out
    
    for chunk in np.array_split(list(map(int, deal_ids)), max(1, len(deal_ids)//40 + 1)):
        params = {
            "filter[OWNER_TYPE_ID]": 2,
            "filter[OWNER_ID]": ",".join(map(str, chunk)),
            "filter[COMPLETED]": "N"
        }
        acts = _bx_get("crm.activity.list", params)
        
        for a in acts:
            out.setdefault(int(a["OWNER_ID"]), []).append(a)
    
    return out

@st.cache_data(ttl=600)
def bx_get_stage_map(stage_ids):
    """Получение информации о стадиях"""
    sort_map, name_map = {}, {}
    
    if not BITRIX24_WEBHOOK or not stage_ids:
        return sort_map, name_map
    
    cats = set()
    for sid in stage_ids:
        if isinstance(sid, str) and sid.startswith("C"):
            try:
                cats.add(int(sid.split(":")[0][1:]))
            except:
                pass
    
    try:
        base = _bx_get("crm.status.list", {"filter[ENTITY_ID]": "DEAL_STAGE"})
        for s in base:
            sort_map[s.get("STATUS_ID")] = int(s.get("SORT", 5000))
            name_map[s.get("STATUS_ID")] = s.get("NAME") or s.get("STATUS_ID")
    except:
        pass
    
    for cid in cats:
        try:
            resp = _bx_get("crm.status.list", {"filter[ENTITY_ID]": f"DEAL_STAGE_{cid}"})
            for s in resp:
                sort_map[s.get("STATUS_ID")] = int(s.get("SORT", 5000))
                name_map[s.get("STATUS_ID")] = s.get("NAME") or s.get("STATUS_ID")
        except:
            continue
    
    return sort_map, name_map

@st.cache_data(ttl=600)
def bx_get_categories():
    """Получение категорий (воронок)"""
    try:
        cats = _bx_get("crm.category.list", {})
        return {int(c["ID"]): c.get("NAME", "Воронка") for c in cats}
    except:
        return {}

@st.cache_data(ttl=600)
def bx_get_timeline_for_deal(deal_id):
    """Получение истории изменений сделки для отслеживания обхода системы"""
    try:
        params = {
            "ENTITY_TYPE": "deal",
            "ENTITY_ID": deal_id
        }
        timeline = _bx_get("crm.timeline.list", params)
        return timeline
    except:
        return []

# ============ UTILS ============
def to_dt(x):
    """Конвертация в datetime"""
    try:
        ts = pd.to_datetime(x, utc=True, errors="coerce")
        if pd.isna(ts):
            return pd.NaT
        return ts.tz_convert(None)
    except:
        return pd.NaT

def days_between(later, earlier):
    """Количество дней между датами"""
    a, b = to_dt(later), to_dt(earlier)
    if pd.isna(a) or pd.isna(b):
        return None
    return max(0, int((a - b) / pd.Timedelta(days=1)))

def get_period_dates(period_type, reference_date=None):
    """
    Получение дат начала и конца для заданного периода
    period_type: 'year', 'quarter', 'month', 'week'
    """
    if reference_date is None:
        reference_date = datetime.now()
    
    if period_type == 'year':
        start = datetime(reference_date.year, 1, 1)
        end = datetime(reference_date.year, 12, 31)
    elif period_type == 'quarter':
        quarter = (reference_date.month - 1) // 3 + 1
        start = datetime(reference_date.year, (quarter - 1) * 3 + 1, 1)
        if quarter == 4:
            end = datetime(reference_date.year, 12, 31)
        else:
            end = datetime(reference_date.year, quarter * 3 + 1, 1) - timedelta(days=1)
    elif period_type == 'month':
        start = datetime(reference_date.year, reference_date.month, 1)
        next_month = start + relativedelta(months=1)
        end = next_month - timedelta(days=1)
    elif period_type == 'week':
        start = reference_date - timedelta(days=reference_date.weekday())
        end = start + timedelta(days=6)
    else:
        start = reference_date
        end = reference_date
    
    return start, end

def get_previous_period(period_type, reference_date=None):
    """Получение предыдущего периода для сравнения"""
    if reference_date is None:
        reference_date = datetime.now()
    
    if period_type == 'year':
        prev_date = datetime(reference_date.year - 1, reference_date.month, reference_date.day)
    elif period_type == 'quarter':
        prev_date = reference_date - relativedelta(months=3)
    elif period_type == 'month':
        prev_date = reference_date - relativedelta(months=1)
    elif period_type == 'week':
        prev_date = reference_date - timedelta(weeks=1)
    else:
        prev_date = reference_date - timedelta(days=1)
    
    return get_period_dates(period_type, prev_date)

# ============ SCORING ============
def compute_health_scores(df, open_tasks_map, stuck_days=5):
    """Вычисление показателей здоровья сделок"""
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
            "lost": str(r.get("STAGE_ID", "")).upper().find("LOSE") >= 0
        }
        
        score = 100
        if flags["no_company"]:
            score -= 10
        if flags["no_contact"]:
            score -= 10
        if flags["no_tasks"]:
            score -= 25
        if flags["stuck"]:
            score -= 25
        if flags["lost"]:
            score = min(score, 15)
        
        opp = float(r.get("OPPORTUNITY") or 0.0)
        prob = float(r.get("PROBABILITY") or 0.0)
        potential = min(100, int((opp > 0) * (30 + min(70, math.log10(max(1, opp))/5 * 70)) * (0.4 + prob/100 * 0.6)))
        
        rows.append({
            "ID сделки": int(r["ID"]),
            "Название": r.get("TITLE", ""),
            "Менеджер ID": int(r.get("ASSIGNED_BY_ID") or 0),
            "Этап ID": r.get("STAGE_ID", ""),
            "Воронка ID": r.get("CATEGORY_ID"),
            "Сумма": opp,
            "Вероятность": prob,
            "Дата создания": create_dt,
            "Дата изменения": to_dt(r.get("DATE_MODIFY")),
            "Последняя активность": last,
            "Начало этапа": begin_dt,
            "Дата закрытия": to_dt(r.get("CLOSEDATE")),
            "Закрыта": r.get("CLOSED") == "Y",
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

def is_success_stage(stage_name, funnel_name):
    """Проверка, является ли стадия успешной"""
    success_stage = SUCCESS_STAGES.get(funnel_name)
    if success_stage:
        return success_stage.lower() in stage_name.lower()
    return False

def is_failure_stage(stage_name, stage_id):
    """Проверка, является ли стадия провальной"""
    # Проверяем по ключевым словам
    failure_keywords = ["проиг", "отказ", "lose", "fail", "недозвон", "спам", "нецелевой"]
    stage_lower = stage_name.lower()
    
    for keyword in failure_keywords:
        if keyword in stage_lower:
            return True
    
    # Проверяем по ID стадии
    if "LOSE" in str(stage_id).upper():
        return True
    
    return False

def get_failure_group(stage_name):
    """Определение группы провала по названию стадии"""
    if stage_name in FAILURE_STAGES_GROUP1["main_stages"]:
        return "Группа 1"
    elif stage_name in FAILURE_STAGES_GROUP2["main_stages"]:
        return "Группа 2"
    return None

# ============ AI ANALYSIS ============
def ai_analyze_health_recommendations(deals_summary, avg_health):
    """AI-рекомендации для поднятия здоровья до 70%"""
    if not PERPLEXITY_API_KEY:
        return "AI-ключ не настроен."
    
    prompt = f"""
Ты эксперт по CRM и управлению продажами. Текущее среднее здоровье сделок: {avg_health:.1f}%.

Статистика сделок:
- Всего сделок: {deals_summary.get('total', 0)}
- Без задач: {deals_summary.get('no_tasks', 0)}
- Без компании: {deals_summary.get('no_company', 0)}
- Без контакта: {deals_summary.get('no_contact', 0)}
- Застрявших: {deals_summary.get('stuck', 0)}
- Проигранных: {deals_summary.get('lost', 0)}

ЗАДАЧА: Дай конкретные пошаговые рекомендации, ЧТО ИМЕННО нужно делать в CRM Bitrix24, чтобы:
1. Поднять здоровье сделок до 70%
2. Поддерживать здоровье не ниже 70%

Рекомендации должны быть:
- Конкретными (какие именно действия в CRM)
- Измеримыми (какие показатели отслеживать)
- Применимыми на практике
- С указанием приоритетов

Формат ответа: краткие bullet points на русском языке.
"""
    
    data = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": "Ты эксперт по CRM-аналитике и продажам."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 1000,
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

def detect_system_bypass(df_scores, timeline_data=None):
    """
    Обнаружение попыток обойти систему
    - Удаление задач
    - Перенос сроков
    - Быстрые изменения стадий
    """
    suspicious_deals = []
    
    # Анализ сделок без задач с недавней активностью
    recent_no_tasks = df_scores[
        (df_scores["Нет задач"]) & 
        (df_scores["Дней без активности"] < 3)
    ]
    
    for _, deal in recent_no_tasks.iterrows():
        suspicious_deals.append({
            "ID сделки": deal["ID сделки"],
            "Название": deal["Название"],
            "Менеджер": deal.get("Менеджер", "Неизвестно"),
            "Подозрение": "Отсутствие задач при недавней активности",
            "Детали": f"Активность {deal['Дней без активности']} дн. назад, но нет открытых задач",
            "Критичность": "Средняя"
        })
    
    # Анализ застрявших сделок с высокой вероятностью
    stuck_high_prob = df_scores[
        (df_scores["Застряла"]) & 
        (df_scores["Вероятность"] > 70)
    ]
    
    for _, deal in stuck_high_prob.iterrows():
        suspicious_deals.append({
            "ID сделки": deal["ID сделки"],
            "Название": deal["Название"],
            "Менеджер": deal.get("Менеджер", "Неизвестно"),
            "Подозрение": "Застой сделки с высокой вероятностью",
            "Детали": f"Вероятность {deal['Вероятность']:.0f}%, но {deal['Дней без активности']} дн. без активности",
            "Критичность": "Высокая"
        })
    
    # Анализ резких изменений здоровья (требует исторических данных)
    # Сейчас используем упрощенную логику
    low_health_high_opp = df_scores[
        (df_scores["Здоровье"] < 50) & 
        (df_scores["Сумма"] > df_scores["Сумма"].quantile(0.75))
    ]
    
    for _, deal in low_health_high_opp.iterrows():
        suspicious_deals.append({
            "ID сделки": deal["ID сделки"],
            "Название": deal["Название"],
            "Менеджер": deal.get("Менеджер", "Неизвестно"),
            "Подозрение": "Крупная сделка с низким здоровьем",
            "Детали": f"Сумма {deal['Сумма']:,.0f} ₽, здоровье {deal['Здоровье']}%",
            "Критичность": "Высокая"
        })
    
    return pd.DataFrame(suspicious_deals) if suspicious_deals else pd.DataFrame()

# ============ ГОДОВОЙ ПЛАН ============
def calculate_yearly_plan(df_scores, yearly_target, current_date=None):
    """
    Расчет выполнения годового плана с прогнозом
    """
    if current_date is None:
        current_date = datetime.now()
    
    year = current_date.year
    
    # Фильтруем только закрытые успешные сделки текущего года
    df_year = df_scores[
        (df_scores["Дата создания"].dt.year == year)
    ].copy()
    
    # Определяем успешные сделки по воронкам
    df_year["Успешна"] = df_year.apply(
        lambda row: is_success_stage(row.get("Название этапа", ""), row.get("Воронка", "")),
        axis=1
    )
    
    # Факт по месяцам
    df_year["Месяц"] = df_year["Дата создания"].dt.month
    df_year["Квартал"] = df_year["Дата создания"].dt.quarter
    
    actual_by_month = df_year[df_year["Успешна"]].groupby("Месяц")["Сумма"].sum()
    actual_by_quarter = df_year[df_year["Успешна"]].groupby("Квартал")["Сумма"].sum()
    
    # Факт нарастающим итогом
    total_actual = actual_by_month.sum()
    
    # План по месяцам (равномерное распределение)
    monthly_plan = yearly_target / 12
    
    # Сколько осталось заработать
    remaining_amount = max(0, yearly_target - total_actual)
    
    # Сколько осталось месяцев
    months_left = 12 - current_date.month + 1
    
    # Скорректированный месячный план для достижения цели
    adjusted_monthly_plan = remaining_amount / months_left if months_left > 0 else 0
    
    # Процент выполнения
    completion_pct = (total_actual / yearly_target * 100) if yearly_target > 0 else 0
    
    # Прогноз на основе потенциала открытых сделок
    open_deals = df_year[~df_year["Успешна"] & ~df_year["Проиграна"]]
    weighted_potential = (open_deals["Сумма"] * open_deals["Вероятность"] / 100).sum()
    
    # Прогноз на конец года
    forecast_total = total_actual + weighted_potential
    forecast_pct = (forecast_total / yearly_target * 100) if yearly_target > 0 else 0
    
    # Формируем результат
    result = {
        "year": year,
        "target": yearly_target,
        "actual": total_actual,
        "remaining": remaining_amount,
        "completion_pct": completion_pct,
        "months_left": months_left,
        "monthly_plan_original": monthly_plan,
        "monthly_plan_adjusted": adjusted_monthly_plan,
        "forecast_total": forecast_total,
        "forecast_pct": forecast_pct,
        "actual_by_month": actual_by_month,
        "actual_by_quarter": actual_by_quarter,
        "weighted_potential": weighted_potential
    }
    
    return result

# ============ SIDEBAR ФИЛЬТРЫ ============
st.sidebar.title("⚙️ Фильтры")

# НИТ (дата начала отслеживания)
nit_date = st.sidebar.date_input(
    "НИТ (с какой даты)", 
    datetime.now().date() - timedelta(days=90),
    help="Начало периода отслеживания"
)

# Выбор типа периода
period_type = st.sidebar.selectbox(
    "Период анализа",
    ["Год", "Квартал", "Месяц", "Неделя"],
    index=2
)

period_map = {
    "Год": "year",
    "Квартал": "quarter", 
    "Месяц": "month",
    "Неделя": "week"
}

selected_period = period_map[period_type]

# Референсная дата для периода
reference_date = st.sidebar.date_input(
    "Референсная дата",
    datetime.now().date(),
    help="Дата для определения текущего периода"
)

# Параметры
stuck_days = st.sidebar.slider("Застряла (дней без активности)", 3, 30, 7)
limit = st.sidebar.slider("Лимит сделок из API", 500, 5000, 2000, step=500)

# Годовой план
st.sidebar.markdown("---")
st.sidebar.subheader("🎯 Годовой план")
yearly_target = st.sidebar.number_input(
    "Цель на год, ₽",
    min_value=0,
    value=50_000_000,
    step=1_000_000,
    format="%d",
    help="Введите целевую выручку на год"
)

# ============ ЗАГРУЗКА ДАННЫХ ============
with st.spinner("Загружаю данные из Bitrix24..."):
    if not BITRIX24_WEBHOOK:
        st.error("❌ Задайте BITRIX24_WEBHOOK в Secrets")
        st.stop()
    
    # Определяем даты для загрузки
    current_period_start, current_period_end = get_period_dates(selected_period, reference_date)
    prev_period_start, prev_period_end = get_previous_period(selected_period, reference_date)
    
    # Загружаем сделки с учетом НИТ
    deals_raw = bx_get_deals(str(nit_date), str(datetime.now().date()), limit=limit)
    
    if not deals_raw:
        st.error("❌ Сделок не найдено за выбранный период.")
        st.stop()
    
    df_raw = pd.DataFrame(deals_raw)
    df_raw["OPPORTUNITY"] = pd.to_numeric(df_raw.get("OPPORTUNITY"), errors="coerce").fillna(0.0)
    
    # Загружаем дополнительные данные
    users_full = bx_get_users_full()
    users_map = {uid: users_full[uid]["name"] for uid in users_full}
    
    open_tasks_map = bx_get_open_activities_for_deal_ids(df_raw["ID"].tolist())
    categories_map = bx_get_categories()
    
    # Вычисляем здоровье
    df_scores = compute_health_scores(df_raw, open_tasks_map, stuck_days=stuck_days)
    
    # Получаем информацию о стадиях
    stage_ids = df_scores["Этап ID"].dropna().unique().tolist()
    sort_map, name_map = bx_get_stage_map(stage_ids)
    
    df_scores["Название этапа"] = df_scores["Этап ID"].map(lambda s: name_map.get(str(s), str(s)))
    df_scores["Менеджер"] = df_scores["Менеджер ID"].map(users_map).fillna("Неизвестно")
    df_scores["Воронка"] = df_scores["Воронка ID"].map(lambda x: categories_map.get(int(x or 0), "Основная"))
    
    # Маркируем успешные и провальные сделки
    df_scores["Успешна"] = df_scores.apply(
        lambda row: is_success_stage(row["Название этапа"], row["Воронка"]),
        axis=1
    )
    
    df_scores["Провал"] = df_scores.apply(
        lambda row: is_failure_stage(row["Название этапа"], row["Этап ID"]),
        axis=1
    )

# Фильтры в сайдбаре по воронкам и менеджерам
funnels = sorted(df_scores["Воронка"].unique())
selected_funnels = st.sidebar.multiselect("Воронки", funnels, default=funnels)

managers = sorted(df_scores["Менеджер"].unique())
selected_managers = st.sidebar.multiselect("Менеджеры", managers, default=managers)

# Применяем фильтры
view_df = df_scores[
    (df_scores["Воронка"].isin(selected_funnels)) &
    (df_scores["Менеджер"].isin(selected_managers))
].copy()

if view_df.empty:
    st.warning("⚠️ Нет данных по выбранным фильтрам.")
    st.stop()

# Фильтруем по текущему и предыдущему периоду
df_current = view_df[
    (view_df["Дата создания"] >= pd.Timestamp(current_period_start)) &
    (view_df["Дата создания"] <= pd.Timestamp(current_period_end))
]

df_previous = view_df[
    (view_df["Дата создания"] >= pd.Timestamp(prev_period_start)) &
    (view_df["Дата создания"] <= pd.Timestamp(prev_period_end))
]

# ============ HEADER ============
st.markdown("# 🟧 БУРМАШ · CRM Дэшборд v5.0")
st.markdown(f"""
**НИТ**: {nit_date} | **Период**: {period_type} ({current_period_start.strftime('%d.%m.%Y')} - {current_period_end.strftime('%d.%m.%Y')})  
**Всего сделок**: {len(view_df):,} | **В текущем периоде**: {len(df_current):,}
""")

# ============ TABS ============
tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "📊 Обзор",
    "⚠️ Проблемы", 
    "👤 По менеджерам",
    "🎯 Градация сделок",
    "⏱️ Время на этапах",
    "💰 Годовой план",
    "🤖 AI-аналитика"
])

# ===== TAB 1: ОБЗОР =====
with tab1:
    st.subheader("📈 Суммарные показатели")
    
    # Метрики с динамикой
    col1, col2, col3, col4 = st.columns(4)
    
    # Количество сделок
    deals_current = len(df_current)
    deals_previous = len(df_previous)
    deals_delta = deals_current - deals_previous
    deals_delta_pct = (deals_delta / deals_previous * 100) if deals_previous > 0 else 0
    
    col1.metric(
        "Сделок",
        f"{deals_current:,}",
        f"{deals_delta:+,} ({deals_delta_pct:+.1f}%)",
        help="Количество сделок в текущем периоде"
    )
    
    # Выручка из успешных сделок трех воронок
    revenue_current = df_current[df_current["Успешна"]]["Сумма"].sum()
    revenue_previous = df_previous[df_previous["Успешна"]]["Сумма"].sum()
    revenue_delta = revenue_current - revenue_previous
    revenue_delta_pct = (revenue_delta / revenue_previous * 100) if revenue_previous > 0 else 0
    
    col2.metric(
        "Выручка, ₽",
        f"{revenue_current:,.0f}",
        f"{revenue_delta:+,.0f} ({revenue_delta_pct:+.1f}%)",
        help="Выручка из успешных сделок 3 воронок"
    )
    
    # Среднее здоровье
    health_current = df_current["Здоровье"].mean()
    health_previous = df_previous["Здоровье"].mean()
    health_delta = health_current - health_previous
    
    col3.metric(
        "Ср. здоровье",
        f"{health_current:.1f}%",
        f"{health_delta:+.1f}%",
        help="Среднее здоровье сделок"
    )
    
    # Средний потенциал
    potential_current = df_current["Потенциал"].mean()
    potential_previous = df_previous["Потенциал"].mean()
    potential_delta = potential_current - potential_previous
    
    col4.metric(
        "Ср. потенциал",
        f"{potential_current:.1f}%",
        f"{potential_delta:+.1f}%",
        help="Средний потенциал сделок"
    )
    
    # Графики динамики
    if px:
        st.markdown("---")
        
        # График количества сделок по периодам
        st.subheader("Динамика сделок")
        
        # Группируем по дням для построения графика
        df_timeline = view_df.copy()
        df_timeline["Дата"] = df_timeline["Дата создания"].dt.date
        
        deals_by_date = df_timeline.groupby("Дата").size().reset_index(name="Количество")
        
        fig_deals = px.line(
            deals_by_date,
            x="Дата",
            y="Количество",
            title="Количество сделок по датам",
            markers=True
        )
        fig_deals.update_layout(height=400)
        st.plotly_chart(fig_deals, use_container_width=True, key="overview_deals_timeline")
        
        # График выручки по воронкам
        st.subheader("Выручка по воронкам")
        
        df_timeline["Дата"] = df_timeline["Дата создания"].dt.date
        revenue_by_funnel_date = df_timeline[df_timeline["Успешна"]].groupby(
            ["Дата", "Воронка"]
        )["Сумма"].sum().reset_index()
        
        fig_revenue = px.line(
            revenue_by_funnel_date,
            x="Дата",
            y="Сумма",
            color="Воронка",
            title="Выручка по воронкам (успешные сделки)",
            markers=True
        )
        fig_revenue.update_layout(height=400)
        st.plotly_chart(fig_revenue, use_container_width=True, key="overview_revenue_timeline")
        
        # График здоровья и потенциала
        st.subheader("Динамика здоровья и потенциала")
        
        health_by_date = df_timeline.groupby("Дата").agg({
            "Здоровье": "mean",
            "Потенциал": "mean"
        }).reset_index()
        
        fig_health = go.Figure()
        fig_health.add_trace(go.Scatter(
            x=health_by_date["Дата"],
            y=health_by_date["Здоровье"],
            name="Среднее здоровье",
            mode="lines+markers",
            line=dict(color="#FF6B35")
        ))
        fig_health.add_trace(go.Scatter(
            x=health_by_date["Дата"],
            y=health_by_date["Потенциал"],
            name="Средний потенциал",
            mode="lines+markers",
            line=dict(color="#4ECDC4")
        ))
        fig_health.update_layout(
            title="Динамика здоровья и потенциала",
            yaxis_title="Процент",
            height=400
        )
        st.plotly_chart(fig_health, use_container_width=True, key="overview_health_timeline")
    
    # Распределение здоровья (воронка с шагом 5%)
    st.markdown("---")
    st.subheader("📊 Распределение здоровья сделок")
    
    # Создаем диапазоны с шагом 5%
    bins = list(range(0, 101, 5))
    df_current["Диапазон здоровья"] = pd.cut(
        df_current["Здоровье"],
        bins=bins,
        labels=[f"{i}-{i+5}%" for i in range(0, 100, 5)],
        include_lowest=True
    )
    
    health_dist = df_current["Диапазон здоровья"].value_counts().sort_index()
    health_dist_df = health_dist.reset_index()
    health_dist_df.columns = ["Диапазон", "Количество"]
    
    if px:
        fig_funnel = go.Figure(go.Funnel(
            y=health_dist_df["Диапазон"],
            x=health_dist_df["Количество"],
            textinfo="value+percent initial"
        ))
        fig_funnel.update_layout(title="Воронка здоровья сделок (шаг 5%)", height=600)
        st.plotly_chart(fig_funnel, use_container_width=True, key="overview_health_funnel")
    
    # Воронки продаж с количеством сделок по этапам
    st.markdown("---")
    st.subheader("🔄 Воронки продаж")
    
    for funnel in selected_funnels:
        if funnel == "Провал":
            continue
            
        df_funnel = df_current[df_current["Воронка"] == funnel]
        
        if df_funnel.empty:
            continue
        
        st.markdown(f"### {funnel}")
        
        # Группируем по этапам
        stage_counts = df_funnel.groupby("Название этапа").size().reset_index(name="Количество")
        
        # Сортируем по количеству
        stage_counts = stage_counts.sort_values("Количество", ascending=False)
        
        col1, col2 = st.columns([2, 1])
        
        with col1:
            if px:
                fig_stages = px.bar(
                    stage_counts,
                    x="Количество",
                    y="Название этапа",
                    orientation="h",
                    title=f"Сделки по этапам: {funnel}"
                )
                st.plotly_chart(fig_stages, use_container_width=True, key=f"funnel_stages_{funnel}")
        
        with col2:
            st.dataframe(stage_counts, use_container_width=True, hide_index=True)
    
    # Анализ провалов
    st.markdown("---")
    st.subheader("❌ Анализ провалов")
    
    df_failures = df_current[df_current["Провал"]]
    
    if not df_failures.empty:
        # Группируем по причинам провала
        failure_reasons = df_failures.groupby("Название этапа").size().reset_index(name="Количество")
        failure_reasons = failure_reasons.sort_values("Количество", ascending=False)
        
        col1, col2 = st.columns([2, 1])
        
        with col1:
            if px:
                fig_failures = px.bar(
                    failure_reasons,
                    x="Количество",
                    y="Название этапа",
                    orientation="h",
                    title="Причины провалов",
                    color="Количество",
                    color_continuous_scale="Reds"
                )
                st.plotly_chart(fig_failures, use_container_width=True, key="overview_failures")
        
        with col2:
            st.metric("Всего провалов", len(df_failures))
            st.dataframe(failure_reasons.head(10), use_container_width=True, hide_index=True)
        
        # Провалы по группам
        st.markdown("#### Провалы по группам стадий")
        
        # Группа 1
        group1_failures = df_failures[
            df_failures["Название этапа"].isin(FAILURE_STAGES_GROUP1["failure_stages"])
        ]
        
        # Группа 2
        group2_failures = df_failures[
            df_failures["Название этапа"].isin(FAILURE_STAGES_GROUP2["failure_stages"])
        ]
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("**Группа 1** (ранние стадии)")
            if not group1_failures.empty:
                g1_reasons = group1_failures.groupby("Название этапа").size().reset_index(name="Количество")
                st.dataframe(g1_reasons, use_container_width=True, hide_index=True)
            else:
                st.info("Нет провалов в этой группе")
        
        with col2:
            st.markdown("**Группа 2** (поздние стадии)")
            if not group2_failures.empty:
                g2_reasons = group2_failures.groupby("Название этапа").size().reset_index(name="Количество")
                st.dataframe(g2_reasons, use_container_width=True, hide_index=True)
            else:
                st.info("Нет провалов в этой группе")
    else:
        st.success("✅ Нет провальных сделок в текущем периоде!")

# ===== TAB 2: ПРОБЛЕМЫ =====
with tab2:
    st.subheader("⚠️ Метрики проблем")
    
    # Считаем проблемы для текущего и предыдущего периода
    problems_current = {
        "Без задач": len(df_current[df_current["Нет задач"]]),
        "Без компании": len(df_current[df_current["Нет компании"]]),
        "Без контакта": len(df_current[df_current["Нет контакта"]]),
        "Застряли": len(df_current[df_current["Застряла"]]),
        "Проиграны": len(df_current[df_current["Провал"]])
    }
    
    problems_previous = {
        "Без задач": len(df_previous[df_previous["Нет задач"]]),
        "Без компании": len(df_previous[df_previous["Нет компании"]]),
        "Без контакта": len(df_previous[df_previous["Нет контакта"]]),
        "Застряли": len(df_previous[df_previous["Застряла"]]),
        "Проиграны": len(df_previous[df_previous["Провал"]])
    }
    
    cols = st.columns(5)
    
    for idx, (problem, count_current) in enumerate(problems_current.items()):
        count_prev = problems_previous[problem]
        delta = count_current - count_prev
        
        with cols[idx]:
            st.metric(
                problem,
                count_current,
                f"{delta:+d}",
                help=f"Изменение по сравнению с предыдущим периодом"
            )
    
    # График распределения проблем по времени
    if px:
        st.markdown("---")
        st.subheader("Распределение проблем по времени")
        
        # Готовим данные
        df_timeline = view_df.copy()
        df_timeline["Дата"] = df_timeline["Дата создания"].dt.date
        
        problems_timeline = []
        
        for date in df_timeline["Дата"].unique():
            df_date = df_timeline[df_timeline["Дата"] == date]
            
            problems_timeline.append({
                "Дата": date,
                "Без задач": len(df_date[df_date["Нет задач"]]),
                "Без компании": len(df_date[df_date["Нет компании"]]),
                "Без контакта": len(df_date[df_date["Нет контакта"]]),
                "Застряли": len(df_date[df_date["Застряла"]]),
                "Проиграны": len(df_date[df_date["Провал"]])
            })
        
        df_problems_timeline = pd.DataFrame(problems_timeline)
        
        # Преобразуем в длинный формат для графика
        df_problems_long = df_problems_timeline.melt(
            id_vars=["Дата"],
            var_name="Тип проблемы",
            value_name="Количество"
        )
        
        fig_problems = px.line(
            df_problems_long,
            x="Дата",
            y="Количество",
            color="Тип проблемы",
            title="Динамика проблем",
            markers=True
        )
        fig_problems.update_layout(height=400)
        st.plotly_chart(fig_problems, use_container_width=True, key="problems_timeline")
    
    # Списки сделок по видам проблем
    st.markdown("---")
    st.subheader("Списки проблемных сделок")
    
    with st.expander(f"❗ Без задач ({problems_current['Без задач']})"):
        df_no_tasks = df_current[df_current["Нет задач"]]
        if not df_no_tasks.empty:
            st.dataframe(
                df_no_tasks[["ID сделки", "Название", "Менеджер", "Воронка", "Сумма", "Здоровье"]],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("Нет сделок без задач")
    
    with st.expander(f"🏢 Без компании ({problems_current['Без компании']})"):
        df_no_company = df_current[df_current["Нет компании"]]
        if not df_no_company.empty:
            st.dataframe(
                df_no_company[["ID сделки", "Название", "Менеджер", "Воронка", "Сумма", "Здоровье"]],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("Нет сделок без компании")
    
    with st.expander(f"📇 Без контакта ({problems_current['Без контакта']})"):
        df_no_contact = df_current[df_current["Нет контакта"]]
        if not df_no_contact.empty:
            st.dataframe(
                df_no_contact[["ID сделки", "Название", "Менеджер", "Воронка", "Сумма", "Здоровье"]],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("Нет сделок без контакта")
    
    with st.expander(f"⏸️ Застрявшие ({problems_current['Застряли']})"):
        df_stuck = df_current[df_current["Застряла"]]
        if not df_stuck.empty:
            st.dataframe(
                df_stuck[["ID сделки", "Название", "Менеджер", "Воронка", "Дней без активности", "Здоровье"]],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("Нет застрявших сделок")
    
    with st.expander(f"❌ Проигранные ({problems_current['Проиграны']})"):
        df_lost = df_current[df_current["Провал"]]
        if not df_lost.empty:
            st.dataframe(
                df_lost[["ID сделки", "Название", "Менеджер", "Воронка", "Название этапа", "Сумма"]],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("Нет проигранных сделок")

# ===== TAB 3: ПО МЕНЕДЖЕРАМ =====
with tab3:
    st.subheader("👥 Аналитика по менеджерам")
    
    mgr_stats = []
    
    for mgr in selected_managers:
        mg_current = df_current[df_current["Менеджер"] == mgr]
        mg_all = view_df[view_df["Менеджер"] == mgr]
        
        if mg_current.empty:
            continue
        
        total = len(mg_current)
        
        # Выручка из успешных сделок 3 воронок
        revenue = mg_current[mg_current["Успешна"]]["Сумма"].sum()
        
        # Среднее здоровье
        avg_health = mg_current["Здоровье"].mean()
        
        # Выиграно (успешные сделки из 3 воронок)
        won = len(mg_current[mg_current["Успешна"]])
        
        # Проиграно (провальные стадии)
        lost = len(mg_current[mg_current["Провал"]])
        
        # Конверсия
        conv_rate = (won / total * 100) if total > 0 else 0
        
        # Качество базы
        base_quality = 100 - (
            mg_current["Нет компании"].sum() + mg_current["Нет контакта"].sum()
        ) / (total * 2) * 100
        
        mgr_stats.append({
            "Менеджер": mgr,
            "Сделок": total,
            "Выручка, ₽": int(revenue),
            "Ср. здоровье, %": round(avg_health, 1),
            "Выиграно": won,
            "Проиграно": lost,
            "Конверсия, %": round(conv_rate, 1),
            "Качество базы, %": round(base_quality, 1)
        })
    
    df_mgr = pd.DataFrame(mgr_stats)
    
    if not df_mgr.empty:
        st.dataframe(df_mgr, use_container_width=True, hide_index=True)
        
        if px:
            st.markdown("---")
            st.subheader("Визуализация по менеджерам")
            
            # Выручка по менеджерам
            fig_mgr_revenue = px.bar(
                df_mgr,
                x="Менеджер",
                y="Выручка, ₽",
                color="Ср. здоровье, %",
                title="Выручка по менеджерам",
                color_continuous_scale="RdYlGn"
            )
            st.plotly_chart(fig_mgr_revenue, use_container_width=True, key="mgr_revenue")
            
            # Сделки vs Конверсия
            fig_mgr_conv = px.scatter(
                df_mgr,
                x="Сделок",
                y="Конверсия, %",
                size="Выручка, ₽",
                hover_data=["Менеджер"],
                title="Сделки vs Конверсия",
                color="Ср. здоровье, %",
                color_continuous_scale="RdYlGn"
            )
            st.plotly_chart(fig_mgr_conv, use_container_width=True, key="mgr_conversion")
        
        # Конверсия по этапам воронки (улучшенная читаемость)
        st.markdown("---")
        st.subheader("Конверсия по этапам воронки")
        
        for mgr in selected_managers:
            mg_data = df_current[df_current["Менеджер"] == mgr]
            
            if mg_data.empty:
                continue
            
            with st.expander(f"👤 {mgr} ({len(mg_data)} сделок)"):
                for funnel in mg_data["Воронка"].unique():
                    mg_funnel = mg_data[mg_data["Воронка"] == funnel]
                    
                    st.markdown(f"**{funnel}**")
                    
                    # Считаем конверсию по этапам
                    stage_counts = mg_funnel.groupby("Название этапа").size()
                    total_deals = len(mg_funnel)
                    
                    stage_conv = pd.DataFrame({
                        "Этап": stage_counts.index,
                        "Сделок": stage_counts.values,
                        "Конверсия, %": (stage_counts.values / total_deals * 100).round(1)
                    })
                    
                    # Сортируем по количеству
                    stage_conv = stage_conv.sort_values("Сделок", ascending=False)
                    
                    col1, col2 = st.columns([2, 1])
                    
                    with col1:
                        if px:
                            fig_conv = px.bar(
                                stage_conv,
                                x="Конверсия, %",
                                y="Этап",
                                orientation="h",
                                text="Сделок"
                            )
                            st.plotly_chart(fig_conv, use_container_width=True, key=f"conv_{mgr}_{funnel}")
                    
                    with col2:
                        st.dataframe(stage_conv, use_container_width=True, hide_index=True)
    else:
        st.info("Нет данных по менеджерам для выбранных фильтров")

# ===== TAB 4: ГРАДАЦИЯ СДЕЛОК =====
with tab4:
    st.subheader("🎯 Градация сделок по здоровью")
    
    # Определяем категории
    quick_wins = df_current[
        (df_current["Здоровье"] >= 70) &
        (df_current["Вероятность"] >= 50) &
        (~df_current["Провал"])
    ]
    
    work_on = df_current[
        (df_current["Здоровье"] >= 40) &
        (df_current["Здоровье"] < 70) &
        (~df_current["Провал"])
    ]
    
    stop_list = df_current[
        (df_current["Здоровье"] < 40) |
        (df_current["Провал"]) |
        ((df_current["Застряла"]) & (df_current["Дней без активности"] > 14))
    ]
    
    col1, col2, col3 = st.columns(3)
    
    col1.metric(
        "🟢 Quick Wins",
        len(quick_wins),
        f"{int(quick_wins['Сумма'].sum()):,} ₽"
    )
    
    col2.metric(
        "🟡 Проработка",
        len(work_on),
        f"{int(work_on['Сумма'].sum()):,} ₽"
    )
    
    col3.metric(
        "🔴 Stop List",
        len(stop_list),
        f"{int(stop_list['Сумма'].sum()):,} ₽"
    )
    
    if px:
        gradation_data = pd.DataFrame({
            "Категория": ["Quick Wins", "Проработка", "Stop List"],
            "Количество": [len(quick_wins), len(work_on), len(stop_list)],
            "Сумма": [
                quick_wins["Сумма"].sum(),
                work_on["Сумма"].sum(),
                stop_list["Сумма"].sum()
            ]
        })
        
        fig_grad = px.bar(
            gradation_data,
            x="Категория",
            y="Количество",
            color="Категория",
            title="Градация сделок",
            color_discrete_map={
                "Quick Wins": "green",
                "Проработка": "orange",
                "Stop List": "red"
            }
        )
        st.plotly_chart(fig_grad, use_container_width=True, key="gradation_chart")
    
    # Списки сделок
    with st.expander(f"🟢 Quick Wins ({len(quick_wins)})"):
        if not quick_wins.empty:
            st.dataframe(
                quick_wins[["ID сделки", "Название", "Менеджер", "Воронка", "Сумма", "Здоровье", "Вероятность"]],
                use_container_width=True,
                hide_index=True
            )
    
    with st.expander(f"🟡 Проработка ({len(work_on)})"):
        if not work_on.empty:
            st.dataframe(
                work_on[["ID сделки", "Название", "Менеджер", "Воронка", "Сумма", "Здоровье", "Потенциал"]],
                use_container_width=True,
                hide_index=True
            )
    
    with st.expander(f"🔴 Stop List ({len(stop_list)})"):
        if not stop_list.empty:
            st.dataframe(
                stop_list[["ID сделки", "Название", "Менеджер", "Воронка", "Название этапа", "Сумма", "Здоровье"]],
                use_container_width=True,
                hide_index=True
            )

# ===== TAB 5: ВРЕМЯ НА ЭТАПАХ =====
with tab5:
    st.subheader("⏱️ Время на этапах воронки")
    
    stage_time = df_current.groupby("Название этапа").agg({
        "Дней на этапе": ["mean", "std", "min", "max", "count"]
    }).round(1)
    
    stage_time.columns = ["Ср. дней", "Откл. (σ)", "Мин", "Макс", "Сделок"]
    stage_time = stage_time.reset_index()
    stage_time = stage_time.sort_values("Сделок", ascending=False)
    
    st.dataframe(stage_time, use_container_width=True, hide_index=True)
    
    if px:
        fig_time = px.bar(
            stage_time.head(10),
            x="Название этапа",
            y="Ср. дней",
            error_y="Откл. (σ)",
            title="Среднее время на этапах (топ-10)",
            color="Ср. дней",
            color_continuous_scale="Blues"
        )
        st.plotly_chart(fig_time, use_container_width=True, key="stage_time")

# ===== TAB 6: ГОДОВОЙ ПЛАН =====
with tab6:
    st.subheader("💰 Годовой план по выручке")
    
    # Рассчитываем план
    plan_data = calculate_yearly_plan(view_df, yearly_target, datetime.now())
    
    # Основные показатели
    col1, col2, col3, col4 = st.columns(4)
    
    col1.metric(
        "Цель на год",
        f"{plan_data['target']:,.0f} ₽"
    )
    
    col2.metric(
        "Выполнено",
        f"{plan_data['actual']:,.0f} ₽",
        f"{plan_data['completion_pct']:.1f}%"
    )
    
    col3.metric(
        "Осталось",
        f"{plan_data['remaining']:,.0f} ₽",
        f"{plan_data['months_left']} мес."
    )
    
    col4.metric(
        "Прогноз",
        f"{plan_data['forecast_total']:,.0f} ₽",
        f"{plan_data['forecast_pct']:.1f}%"
    )
    
    # План на оставшиеся периоды
    st.markdown("---")
    st.subheader("План на оставшиеся периоды")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.metric(
            "Месячный план (исходный)",
            f"{plan_data['monthly_plan_original']:,.0f} ₽"
        )
    
    with col2:
        st.metric(
            "Скорректированный месячный план",
            f"{plan_data['monthly_plan_adjusted']:,.0f} ₽",
            help="Чтобы выполнить годовой план"
        )
    
    # Графики
    if px:
        st.markdown("---")
        
        # График факт vs план по месяцам
        months_names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", 
                       "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
        
        monthly_data = []
        for month in range(1, 13):
            actual = plan_data['actual_by_month'].get(month, 0)
            plan = plan_data['monthly_plan_original']
            
            monthly_data.append({
                "Месяц": months_names[month-1],
                "Факт": actual,
                "План": plan,
                "Выполнение, %": (actual / plan * 100) if plan > 0 else 0
            })
        
        df_monthly = pd.DataFrame(monthly_data)
        
        fig_monthly = go.Figure()
        
        fig_monthly.add_trace(go.Bar(
            x=df_monthly["Месяц"],
            y=df_monthly["Факт"],
            name="Факт",
            marker_color="#4ECDC4"
        ))
        
        fig_monthly.add_trace(go.Scatter(
            x=df_monthly["Месяц"],
            y=df_monthly["План"],
            name="План",
            mode="lines+markers",
            marker_color="#FF6B35",
            line=dict(dash="dash")
        ))
        
        fig_monthly.update_layout(
            title="Факт vs План по месяцам",
            yaxis_title="Сумма, ₽",
            height=400
        )
        
        st.plotly_chart(fig_monthly, use_container_width=True, key="yearly_plan_monthly")
        
        # Таблица по месяцам
        st.dataframe(df_monthly, use_container_width=True, hide_index=True)
        
        # Прогноз
        st.markdown("---")
        st.subheader("📊 Прогноз на конец года")
        
        st.info(f"""
        **Прогноз основан на:**
        - Фактическая выручка: {plan_data['actual']:,.0f} ₽
        - Взвешенный потенциал открытых сделок: {plan_data['weighted_potential']:,.0f} ₽
        - Прогнозируемая итоговая выручка: {plan_data['forecast_total']:,.0f} ₽
        - Выполнение плана: {plan_data['forecast_pct']:.1f}%
        """)
        
        if plan_data['forecast_pct'] >= 100:
            st.success("✅ Прогнозируется выполнение годового плана!")
        elif plan_data['forecast_pct'] >= 80:
            st.warning("⚠️ Есть риск недовыполнения плана. Требуется усиление работы.")
        else:
            st.error("❌ Высокий риск невыполнения плана. Требуются срочные меры!")

# ===== TAB 7: AI-АНАЛИТИКА =====
with tab7:
    st.subheader("🤖 AI-аналитика и рекомендации")
    
    # AI-рекомендации по здоровью
    st.markdown("### Рекомендации по здоровью сделок")
    
    avg_health = view_df["Здоровье"].mean()
    
    deals_summary = {
        "total": len(view_df),
        "no_tasks": view_df["Нет задач"].sum(),
        "no_company": view_df["Нет компании"].sum(),
        "no_contact": view_df["Нет контакта"].sum(),
        "stuck": view_df["Застряла"].sum(),
        "lost": view_df["Провал"].sum()
    }
    
    with st.spinner("Генерирую AI-рекомендации..."):
        recommendations = ai_analyze_health_recommendations(deals_summary, avg_health)
        st.markdown(recommendations)
    
    # Отслеживание обхода системы
    st.markdown("---")
    st.markdown("### 🚨 Отслеживание попыток обхода системы")
    
    suspicious = detect_system_bypass(view_df)
    
    if not suspicious.empty:
        st.warning(f"⚠️ Обнаружено {len(suspicious)} подозрительных сделок")
        
        # Группируем по критичности
        for criticality in ["Высокая", "Средняя"]:
            crit_deals = suspicious[suspicious["Критичность"] == criticality]
            
            if not crit_deals.empty:
                with st.expander(f"⚠️ {criticality} критичность ({len(crit_deals)})"):
                    st.dataframe(
                        crit_deals[["ID сделки", "Название", "Менеджер", "Подозрение", "Детали"]],
                        use_container_width=True,
                        hide_index=True
                    )
    else:
        st.success("✅ Подозрительной активности не обнаружено")
    
    # AI-анализ по менеджерам
    st.markdown("---")
    st.markdown("### 👥 AI-анализ по менеджерам")
    
    for mgr in selected_managers[:3]:  # Ограничиваем до 3 менеджеров для экономии API запросов
        mg_data = view_df[view_df["Менеджер"] == mgr]
        
        if mg_data.empty:
            continue
        
        mgr_summary = {
            "total_deals": len(mg_data),
            "revenue": int(mg_data[mg_data["Успешна"]]["Сумма"].sum()),
            "avg_health": int(mg_data["Здоровье"].mean()),
            "no_tasks": int(mg_data["Нет задач"].sum()),
            "stuck": int(mg_data["Застряла"].sum()),
            "won": int(mg_data["Успешна"].sum()),
            "lost": int(mg_data["Провал"].sum())
        }
        
        with st.expander(f"👤 {mgr} ({len(mg_data)} сделок)"):
            if PERPLEXITY_API_KEY:
                with st.spinner(f"Анализирую {mgr}..."):
                    prompt = f"""
Ты эксперт по продажам. Проанализируй работу менеджера:

Менеджер: {mgr}
Сделок: {mgr_summary['total_deals']}
Выручка: {mgr_summary['revenue']:,} ₽
Ср. здоровье: {mgr_summary['avg_health']}%
Выиграно: {mgr_summary['won']}
Проиграно: {mgr_summary['lost']}
Без задач: {mgr_summary['no_tasks']}
Застряло: {mgr_summary['stuck']}

Дай краткий анализ (2-3 абзаца):
1. Сильные стороны
2. Проблемные зоны
3. Конкретные рекомендации

Формат: bullet points на русском.
"""
                    
                    data = {
                        "model": "sonar-pro",
                        "messages": [
                            {"role": "system", "content": "Ты эксперт по CRM и продажам."},
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
                        analysis = result["choices"][0]["message"]["content"]
                        st.markdown(analysis)
                    except Exception as e:
                        st.error(f"Ошибка AI: {str(e)}")
            else:
                st.info("AI-ключ не настроен")

# ============ FOOTER ============
st.markdown("---")
st.caption("БУРМАШ · CRM Дэшборд v5.0 | Powered by Bitrix24 + Perplexity AI")
