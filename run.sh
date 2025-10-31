#!/usr/bin/env bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run burmash_crm_dashboard_v6.py
