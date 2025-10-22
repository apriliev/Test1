@echo off
python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
streamlit run burmash_crm_dashboard_v6.py
