# estoque_YV.py
# Fluxo:
# 1) QR fixo abre o app (home)
# 2) Login (USUARIOS)
# 3) Seleciona modo: ENTRADA, SAIDA, INVENTARIO
# 4) Escaneia QR do item (URL com ?item=PR001) e registra em lote
# 5) Confirma e já fica pronto para o próximo scan

import streamlit as st
import pandas as pd
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="YV Estoque", layout="centered")

SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
TZ = ZoneInfo("America/Sao_Paulo")


def now_local_iso() -> str:
    # Ex: 2026-02-06T09:18:00-03:00
    return datetime.now(TZ).isoformat(timespec="seconds")


def gs_client():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPE
    )
    return gspread.authorize(creds)


def normalize_cell(v):
    if v is None:
        return ""

    try:
        import numpy as np
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            return float(v)
        if isinstance(v, (np.bool_,)):
            return bool(v)
    except Exception:
        pass

    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except Exception:
        pass

    try:
        if isinstance(v, (pd.Timestamp,)):
            return v.isoformat()
    except Exception:
        pass

    if isinstance(v, (int, float, str, bool)):
        return v

    return str(v)


@st.cache_data(ttl=8)
def read_sheet_df(sheet_name: str) -> pd.DataFrame:
    sh = gs_client().open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet(sheet_name)
    return pd.DataFrame(ws.get_all_records())


def append_row(sheet_name: str, row: dict):
    sh = gs_client().open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet(sheet_name)
    headers = ws.row_values(1)

    values = [normalize_cell(row.get(h, "")) for h in headers]
    ws.append_row(values, value_input_option="USER_ENTERED")


def is_active_flag(x) -> bool:
    return str(x).strip().lower() in ["1", "true", "sim", "yes", "y"]


def calc_saldos(trans_df: pd.DataFrame) -> pd.DataFrame:
    if trans_df is None or trans_df.empty:
        return pd.DataFrame(columns=["item_id", "saldo_atual"])

    df = trans_df.copy()
    if "quantidade_efetiva" not in df.columns:
        df["quantidade_efetiva"] = 0

    df["item_id"] = df["item_id"].astype(str)
    df["quantidade_efetiva"] = pd.to_numeric(df["quantidade_efetiva"], errors="coerce").fillna(0.0)

    out = df.groupby("item_id", as_index=False)["quantidade_efetiva"].sum()
    out = out.rename(columns={"quantidade_efetiva": "saldo_atual"})
    return out


def get_item(itens_df: pd.DataFrame, item_id: str) -> pd.Series | None:
    if itens_df is None or itens_df.empty or "item_id" not in itens_df.columns:
        return None
    df = itens_df.copy()
    df["item_id"] = df["item_id"].astype(str)
    rows = df[df["item_id"] == str(item_id)]
    if rows.empty:
        return None
    return rows.iloc[0]


def get_saldo(saldos_df: pd.DataFrame, item_id: str) -> float:
    if saldos_df is None or saldos_df.empty:
        return 0.0
    r = saldos_df[saldos_df["item_id"] == str(item_id)]
    if r.empty:
        return 0.0
    return float(r["saldo_atual"].iloc[0])


def toast_ok(msg: str):
    # st.toast é ótimo, mas se não estiver disponível na versão, cai no st.success
    try:
        st.toast(msg, icon="✅")
    except Exception:
        st.success(msg)


def set_mode(mode: str):
    st.session_state["mode"] = mode


def mode_selector():
    st.subheader("Modo de operação")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Entrada", use_container_width=True):
            set_mode("ENTRADA")
            st.rerun()
    with c2:
        if st.button("Saida", use_container_width=True):
            set_mode("SAIDA")
            st.rerun()
    with c3:
        if st.button("Inventario", use_container_width=True):
            set_mode("INVENTARIO")
            st.rerun()

    st.caption(f"Modo atual: {st.session_state.get('mode','ENTRADA')}")


# -------------------------
# Load users and login
# -------------------------
users_df = read_sheet_df("USUARIOS")
if users_df is None or users_df.empty:
    st.error("Aba USUARIOS está vazia ou não foi encontrada.")
    st.stop()

if "ativo" in users_df.columns:
    users_df["ativo_norm"] = users_df["ativo"].apply(is_active_flag)
else:
    users_df["ativo_norm"] = True

users_active = users_df[users_df["ativo_norm"]].copy()
if users_active.empty:
    st.error("Nenhum usuário ativo cadastrado na aba USUARIOS.")
    st.stop()

if "user_id" not in st.session_state:
    st.title("Login")

    nomes = users_active["nome"].astype(str).tolist()
    nome = st.selectbox("Usuário", nomes)
    pin = st.text_input("PIN", type="password")

    if st.button("Entrar"):
        u = users_active[users_active["nome"].astype(str) == str(nome)].iloc[0]
        if str(pin).strip() == str(u.get("pin", "")).strip():
            st.session_state["user_id"] = str(u.get("user_id", nome))
            st.session_state["user_nome"] = str(u.get("nome", nome))
            st.session_state["perfil"] = str(u.get("perfil", ""))
            st.session_state.setdefault("mode", "ENTRADA")
            toast_ok("Login realizado")
            st.rerun()
        else:
            st.error("PIN incorreto")
    st.stop()

# Header
st.caption(f"Logado: {st.session_state.get('user_nome','')}")

# -------------------------
# Load data
# -------------------------
itens_df = read_sheet_df("ITENS")
trans_df = read_sheet_df("TRANSACOES")
saldos_df = calc_saldos(trans_df)

# -------------------------
# Mode selector always visible
# -------------------------
mode_selector()

# -------------------------
# Routing by query param item
# -------------------------
qp = st.query_params
item_id = qp.get("item", None)

# Quick instructions
with st.expander("Como usar (rápido)"):
    st.write("1) Escolha um modo acima (Entrada, Saida ou Inventario).")
    st.write("2) Escaneie o QR do item. O QR deve abrir o link com ?item=ID do item.")
    st.write("3) Informe quantidade e confirme. Repita para o próximo item.")
    st.write("Dica: mantenha esta tela aberta e só vá escaneando os QRs na sequência.")

# If no item scanned yet, show a lightweight panel
if not item_id:
    st.subheader("Aguardando scan de item")
    st.info("Escaneie o QR de um item para registrar. O QR deve abrir esta URL com ?item=SEU_ITEM_ID.")
    st.stop()

item_id = str(item_id).strip()
item = get_item(itens_df, item_id)
if item is None:
    st.error(f"Item não encontrado: {item_id}")
    st.stop()

nome_item = str(item.get("nome", item_id))
unidade = str(item.get("unidade", ""))
localizacao = str(item.get("localizacao", ""))
categoria = str(item.get("categoria", ""))

saldo_atual = get_saldo(saldos_df, item_id)

st.title(nome_item)
meta = st.columns(4)
with meta[0]:
    st.caption(f"ID: {item_id}")
with meta[1]:
    st.caption(f"Unidade: {unidade}")
with meta[2]:
    st.caption(f"Local: {localizacao}")
with meta[3]:
    st.caption(f"Saldo: {saldo_atual:g}")

mode = st.session_state.get("mode", "ENTRADA")

# Default qty
if "last_qty" not in st.session_state:
    st.session_state["last_qty"] = 1.0

qtd = st.number_input("Quantidade", min_value=0.0, step=1.0, value=float(st.session_state["last_qty"]))

obs = st.text_input("Observação (opcional)", value="")

# INVENTARIO: opção de registrar contagem física que gera ajuste automático
if mode == "INVENTARIO":
    st.caption("Inventário: informe a quantidade contada fisicamente. O sistema calcula diferença e registra ajuste automaticamente.")
    if st.button("Confirmar contagem", use_container_width=True):
        qtd_f = float(qtd)
        if qtd_f < 0:
            st.error("Quantidade inválida.")
            st.stop()

        saldo_teorico = float(saldo_atual)
        diferenca = float(qtd_f - saldo_teorico)

        # Sempre registra contagem
        cont_row = {
            "contagem_id": str(uuid.uuid4()),
            "timestamp": now_local_iso(),
            "item_id": str(item_id),
            "saldo_teorico_no_momento": float(saldo_teorico),
            "quantidade_contada": float(qtd_f),
            "diferenca": float(diferenca),
            "user_id": str(st.session_state.get("user_id", "")),
        }
        try:
            append_row("CONTAGENS", cont_row)
        except Exception:
            # Se você não quiser usar CONTAGENS, pode deixar a aba existir vazia
            pass

        # Se houver diferença, registra AJUSTE em transações
        if abs(diferenca) > 1e-9:
            sinal_store = 1 if diferenca > 0 else -1
            trans_row = {
                "trans_id": str(uuid.uuid4()),
                "timestamp": now_local_iso(),
                "item_id": str(item_id),
                "acao": "AJUSTE",
                "sinal": int(sinal_store),
                "quantidade": float(abs(diferenca)),
                "quantidade_efetiva": float(diferenca),
                "user_id": str(st.session_state.get("user_id", "")),
                "obs": (f"Ajuste inventário. Contado {qtd_f:g}, teórico {saldo_teorico:g}. " + str(obs)).strip(),
            }
            append_row("TRANSACOES", trans_row)

        st.session_state["last_qty"] = 1.0
        toast_ok("Item registrado no inventário")
        # Mantém a tela pronta para o próximo scan
        st.query_params.clear()
        st.rerun()

else:
    # ENTRADA ou SAIDA
    label = "Confirmar entrada" if mode == "ENTRADA" else "Confirmar saída"
    if st.button(label, use_container_width=True):
        qtd_f = float(qtd)
        if qtd_f <= 0:
            st.error("Quantidade precisa ser maior que zero.")
            st.stop()

        if mode == "ENTRADA":
            efetiva = float(qtd_f)
            acao = "ENTRADA"
            sinal_store = 1
        else:
            efetiva = -float(qtd_f)
            acao = "SAIDA"
            sinal_store = -1

        row = {
            "trans_id": str(uuid.uuid4()),
            "timestamp": now_local_iso(),
            "item_id": str(item_id),
            "acao": str(acao),
            "sinal": int(sinal_store),
            "quantidade": float(qtd_f),
            "quantidade_efetiva": float(efetiva),
            "user_id": str(st.session_state.get("user_id", "")),
            "obs": str(obs).strip(),
        }

        append_row("TRANSACOES", row)
        st.session_state["last_qty"] = 1.0
        toast_ok("Item registrado com sucesso")
        # Pronto para o próximo scan no mesmo modo
        st.query_params.clear()
        st.rerun()

# Botão de logout simples
st.divider()
if st.button("Sair do login"):
    for k in ["user_id", "user_nome", "perfil"]:
        if k in st.session_state:
            del st.session_state[k]
    st.rerun()
