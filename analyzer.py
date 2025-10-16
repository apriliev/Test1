import os
import requests
import json
import time

BITRIX24_WEBHOOK = os.getenv('BITRIX24_WEBHOOK')
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_API_KEY = os.getenv('PERPLEXITY_API_KEY')

class SimpleAnalyzer:
    def __init__(self):
        if not BITRIX24_WEBHOOK or not PERPLEXITY_API_KEY:
            raise ValueError("Проверьте Secrets на Streamlit — нужны BITRIX24_WEBHOOK и PERPLEXITY_API_KEY")
        self.webhook = BITRIX24_WEBHOOK.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        }

    def test_connections(self):
        r = requests.get(self.webhook + "/profile.json")
        print("Bitrix профиль ->", r.json())
        data = {
            "model": "sonar-pro",
            "messages": [
                {"role": "system", "content": "Кратко, по делу."},
                {"role": "user", "content": "Тестовое подключение к Perplexity PRO. Привет!"}
            ],
            "max_tokens": 50,
            "temperature": 0.2
        }
        resp = requests.post(PERPLEXITY_API_URL, headers=self.headers, json=data)
        print("Perplexity raw:", resp.text)
        return True

    def get_deals(self, date_from=None, date_to=None, limit=50, pause_sec=1.0):
        deals, start = [], 0
        params = {
            "select[]": [
                "ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "ASSIGNED_BY_ID",
                "DATE_CREATE", "DATE_MODIFY", "LAST_ACTIVITY_TIME", "PROBABILITY"
            ]
        }
        if date_from:
            params["filter[>=DATE_CREATE]"] = date_from
        if date_to:
            params["filter[<=DATE_CREATE]"] = date_to

        while True:
            params["start"] = start
            r = requests.get(self.webhook + "/crm.deal.list.json", params=params)
            res = r.json()
            print(f"Пакет с {start}, загружено: {len(res.get('result', []))}")
            if res.get("result"):
                print("Пример сделки:", res["result"][0])
            else:
                break
            deals.extend(res.get("result", []))
            start += 50
            if len(deals) >= limit or len(res["result"]) < 50:
                break
            time.sleep(pause_sec)
        print(f"Всего получено сделок: {len(deals)}")
        return deals[:limit]

    def run_ai_analysis(self, deals):
        if not deals:
            return {"health_score": 0,
                    "summary": "Нет данных для анализа",
                    "recommendations": ["Добавьте сделки в CRM"]}
        sample = deals[:10]
        prompt = f"""
Ты эксперт по CRM-аналитике. Проанализируй сделки из Битрикс24.

Всего сделок: {len(deals)}
Примеры:
{json.dumps(sample, ensure_ascii=False, indent=2)}

Ответ строго в формате JSON:
{{
  "health_score": 88
