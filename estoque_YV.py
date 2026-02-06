# estoque_YV.py
# App Streamlit: controle de estoque por QR (1 QR por item)
# Funcionalidades:
# - Login simples (USUARIOS: nome + pin + ativo)
# - Tela do item via URL ?item=PR001
# - Entrada, Saida, Ajuste (ajuste positivo/negativo)
# - Registro em TRANSACOES
# - Dashboard (sem item): lista saldos, abaixo do minimo, sugestao de compra

import streamlit as st
import pandas as pd
import uuid
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="YV Estoque", layout="centered")

SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def gs_client():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPE
    )
    return gspread.authorize(creds)


def _to_int(v, default=0):
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return int(v)
        s = str(v).strip()
        if s == "":
            return default
        # aceita "1", "1.0"
        return int(float(s.replace(",", ".")))
    except Exception:
        return default


def _to_float(v, default=0.0):
    try:
        if v is None:
            return default
        s = str(v).strip()
        if s == "":
            return default
        return float(s.replace(",", "."))
    except Exception:
        return default


def normalize_cell(v):
    """
    Converte qualquer tipo estranho (numpy, pandas, Decimal, etc)
    para tipos simples que o requests/json consegue serializar.
    """
    if v is None:
        return ""

    # numpy
    try:
        import numpy as np  # type: ignore

        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            return float(v)
        if isinstance(v, (np.bool_,)):
            return bool(v)
    except Exception:
        pass

    # Decimal
    try:
        from decimal import Decimal  # type: ignore

        if isinstance(v, Decimal):
            return float(v)
    except Exception:
        pass

    # pandas timestamp
    try:
        import pandas as _pd  # type: ignore

        if isinstance(v, (_pd.Timestamp,)):
            return v.isoformat()
    except Exception:
        pass

    if isinstance(v, (int, float, str, bool)):
        return v

    return str(v)


@st.cache_data(ttl=10)
def read_sheet_df(sheet_name: str) -> pd.DataFrame:
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet(sheet_name)
    rows = ws.get_all_records()
    return pd.DataFrame(rows)


def append_row(sheet_name: str, row: dict):
    gc = gs_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet(sheet_name)
    headers = ws.row_values(1)

    values = [normalize_cell(row.get(h, "")) for h in headers]
    ws.append_row(values, value_input_option="USER_ENTERED")


def calc_saldos(trans_df: pd.DataFrame) -> pd.DataFrame:
    if trans_df is None or trans_df.empty:
        return pd.DataFrame(columns=["item_id", "saldo_atual"])

    df = trans_df.copy()

    if "quantidade_efetiva" not in df.columns:
        df["quantidade_efetiva"] = 0

    df["item_id"] = df["item_id"].astype(str)
    df["quantidade_efetiva"] = pd.to_numeric(df["quantidade_efetiva"], errors="coerce").fillna(0.0)

    saldos = df.groupby("item_id", as_index=False)["quantidade_efetiva"].sum()
    saldos = saldos.rename(columns={"quantidade_efetiva": "saldo_atual"})
    return saldos


def build_dashboard(itens_df: pd.DataFrame, saldos_df: pd.DataFrame) -> pd.DataFrame:
    itens = itens_df.copy()
    itens["item_id"] = itens["item_id"].astype(str)

    # ativo pode vir como 1/0, "1"/"0", "TRUE"/"FALSE"
    if "ativo" in itens.columns:
        itens["ativo_norm"] = itens["ativo"].apply(lambda x: 1 if str(x).strip().lower() in ["1", "true", "sim", "yes"] else 0)
    else:
        itens["ativo_norm"] = 1

    itens = itens[itens["ativo_norm"] == 1].copy()

    out = itens.merge(saldos_df, on="item_id", how="left")
    out["saldo_atual"] = out["saldo_atual"].fillna(0.0)

    if "estoque_min" not in out.columns:
        out["estoque_min"] = 0
    if "estoque_alvo" not in out.columns:
        out["estoque_alvo"] = 0

    out["estoque_min"] = out["estoque_min"].apply(_to_float)
    out["estoque_alvo"] = out["estoque_alvo"].apply(_to_float)
    out["abaixo_min"] = (out["saldo_atual"] < out["estoque_min"]).astype(int)
    out["sugestao_compra"] = (out["estoque_alvo"] - out["saldo_atual"]).clip(lower=0)

    cols = ["item_id", "nome", "categoria", "unidade", "localizacao", "saldo_atual", "estoque_min", "estoque_alvo", "abaixo_min", "sugestao_compra"]
    for c in cols:
        if c not in out.columns:
            out[c] = ""

    return out[cols].sort_values(["abaixo_min", "sugestao_compra", "nome"], ascending=[False, False, True])


# -------------------------
# UI: Login
# -------------------------
try:
    users_df = read_sheet_df("USUARIOS")
except Exception as e:
    st.error("Nao consegui ler a aba USUARIOS. Verifique nome da aba e permissao da planilha.")
    st.exception(e)
    st.stop()

if users_df.empty:
    st.error("Aba USUARIOS esta vazia.")
    st.stop()

users_df["ativo_norm"] = users_df.get("ativo", 1).apply(lambda x: 1 if str(x).strip().lower() in ["1", "true", "sim", "yes"] else 0)
users_active = users_df[users_df["ativo_norm"] == 1].copy()

if "user_id" not in st.session_state:
    st.title("Login")

    nomes = users_active["nome"].astype(str).tolist()
    nome = st.selectbox("Usuario", nomes)
    pin = st.text_input("PIN", type="password")

    if st.button("Entrar"):
        u = users_active[users_active["nome"].astype(str) == str(nome)].iloc[0]
        pin_db = str(u.get("pin", "")).strip()
        if str(pin).strip() == pin_db:
            st.session_state["user_id"] = str(u.get("user_id", nome))
            st.session_state["user_nome"] = str(u.get("nome", nome))
            st.session_state["perfil"] = str(u.get("perfil", ""))
            st.success("Ok")
            st.rerun()
        else:
            st.error("PIN incorreto")
    st.stop()

st.caption(f"Logado: {st.session_state.get('user_nome','')}")

# -------------------------
# Load ITENS + TRANSACOES
# -------------------------
try:
    itens_df = read_sheet_df("ITENS")
except Exception as e:
    st.error("Nao consegui ler a aba ITENS. Verifique nome da aba e permissao da planilha.")
    st.exception(e)
    st.stop()

try:
    trans_df = read_sheet_df("TRANSACOES")
except Exception:
    # Se ainda nao existe, segue com vazio
    trans_df = pd.DataFrame(columns=["item_id", "quantidade_efetiva"])

saldos_df = calc_saldos(trans_df)

# -------------------------
# Routing: Dashboard or Item screen
# -------------------------
qp = st.query_params
item_id = qp.get("item", None)

if not item_id:
    st.title("Dashboard")

    dash = build_dashboard(itens_df, saldos_df)

    st.subheader("Abaixo do minimo")
    st.dataframe(dash[dash["abaixo_min"] == 1], use_container_width=True)

    st.subheader("Sugestao de compra")
    st.dataframe(dash[dash["sugestao_compra"] > 0].sort_values("sugestao_compra", ascending=False), use_container_width=True)

    st.subheader("Todos os itens ativos")
    st.dataframe(dash, use_container_width=True)

    st.caption("Dica: abra um item com ?item=PR001 para registrar movimentos via QR.")
    st.stop()

# -------------------------
# Item screen
# -------------------------
item_id = str(item_id).strip()
if itens_df.empty or "item_id" not in itens_df.columns:
    st.error("Aba ITENS nao tem coluna item_id.")
    st.stop()

itens_df["item_id"] = itens_df["item_id"].astype(str)
item_rows = itens_df[itens_df["item_id"] == item_id]
if item_rows.empty:
    st.error(f"Item nao encontrado: {item_id}")
    st.stop()

item = item_rows.iloc[0]
nome_item = str(item.get("nome", item_id))
categoria = str(item.get("categoria", ""))
unidade = str(item.get("unidade", ""))
localizacao = str(item.get("localizacao", ""))

saldo_row = saldos_df[saldos_df["item_id"] == item_id]
saldo_atual = float(saldo_row["saldo_atual"].iloc[0]) if not saldo_row.empty else 0.0

st.title(nome_item)
info_cols = st.columns(3)
with info_cols[0]:
    st.caption(f"Item: {item_id}")
with info_cols[1]:
    st.caption(f"Unidade: {unidade}")
with info_cols[2]:
    st.caption(f"Local: {localizacao}")

st.write(f"Saldo atual: {saldo_atual:g}")

acao = st.radio("Acao", ["ENTRADA", "SAIDA", "AJUSTE"], horizontal=True)
qtd = st.number_input("Quantidade", min_value=0.0, step=1.0, value=1.0)

aj_sinal = 1
if acao == "AJUSTE":
    aj = st.radio("Ajuste", ["Positivo", "Negativo"], horizontal=True)
    aj_sinal = 1 if aj == "Positivo" else -1

obs = st.text_input("Observacao (opcional)")

if st.button("Confirmar"):
    qtd_f = float(qtd)
    if qtd_f <= 0:
        st.error("Quantidade precisa ser maior que zero.")
        st.stop()

    if acao == "ENTRADA":
        efetiva = qtd_f
        sinal_store = 1
    elif acao == "SAIDA":
        efetiva = -qtd_f
        sinal_store = -1
    else:
        efetiva = qtd_f * int(aj_sinal)
        sinal_store = int(aj_sinal)

    row = {
        "trans_id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "item_id": str(item_id),
        "acao": str(acao),
        "sinal": int(sinal_store),
        "quantidade": float(qtd_f),
        "quantidade_efetiva": float(efetiva),
        "user_id": str(st.session_state.get("user_id", "")),
        "obs": str(obs).strip(),
    }

    try:
        append_row("TRANSACOES", row)
        st.success("Registrado com sucesso.")
        st.rerun()
    except Exception as e:
        st.error("Falha ao gravar na aba TRANSACOES. Verifique se a aba existe e se tem cabecalho na linha 1.")
        st.exception(e)
