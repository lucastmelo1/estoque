
import streamlit as st
import pandas as pd
import uuid
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Estoque QR", layout="centered")

SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]

def client():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPE
    )
    return gspread.authorize(creds)

def read_df(sheet):
    ws = client().open_by_key(SPREADSHEET_ID).worksheet(sheet)
    return pd.DataFrame(ws.get_all_records())

def append_row(sheet, row):
    ws = client().open_by_key(SPREADSHEET_ID).worksheet(sheet)
    headers = ws.row_values(1)
    ws.append_row([row.get(h, "") for h in headers])

def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

users = read_df("USUARIOS")
if "user_id" not in st.session_state:
    st.title("Login")
    nome = st.selectbox("Usuário", users["nome"])
    pin = st.text_input("PIN", type="password")
    if st.button("Entrar"):
        u = users[users["nome"] == nome].iloc[0]
        if str(pin) == str(u["pin"]):
            st.session_state["user_id"] = u["user_id"]
            st.session_state["user_nome"] = u["nome"]
            st.rerun()
        else:
            st.error("PIN inválido")
    st.stop()

itens = read_df("ITENS")
trans = read_df("TRANSACOES")

if not trans.empty:
    trans["quantidade_efetiva"] = pd.to_numeric(trans["quantidade_efetiva"], errors="coerce").fillna(0)
    saldos = trans.groupby("item_id")["quantidade_efetiva"].sum().reset_index()
else:
    saldos = pd.DataFrame(columns=["item_id","quantidade_efetiva"])

qp = st.query_params
item_id = qp.get("item", None)

st.caption(f"Logado: {st.session_state['user_nome']}")

if not item_id:
    st.title("Dashboard")
    st.dataframe(saldos)
    st.stop()

item = itens[itens["item_id"] == item_id].iloc[0]
saldo = saldos[saldos["item_id"] == item_id]["quantidade_efetiva"]
saldo = float(saldo.iloc[0]) if not saldo.empty else 0

st.title(item["nome"])
st.caption(f"Saldo atual: {saldo}")

acao = st.radio("Ação", ["ENTRADA","SAIDA","AJUSTE"], horizontal=True)
qtd = st.number_input("Quantidade", min_value=0.0, step=1.0)
sinal = 1
if acao == "AJUSTE":
    sinal = 1 if st.radio("Ajuste", ["Positivo","Negativo"]) == "Positivo" else -1

if st.button("Confirmar"):
    if qtd <= 0:
        st.error("Quantidade inválida")
    else:
        efetiva = qtd if acao=="ENTRADA" else -qtd if acao=="SAIDA" else qtd*sinal
        append_row("TRANSACOES", {
            "trans_id": str(uuid.uuid4()),
            "timestamp": now(),
            "item_id": item_id,
            "acao": acao,
            "sinal": sinal,
            "quantidade": qtd,
            "quantidade_efetiva": efetiva,
            "user_id": st.session_state["user_id"],
            "obs": ""
        })
        st.success("Registrado")
        st.rerun()
