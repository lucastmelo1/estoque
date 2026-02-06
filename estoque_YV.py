import streamlit as st
import pandas as pd
import uuid
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials

from streamlit_cookies_manager import EncryptedCookieManager

st.set_page_config(page_title="YV Estoque", layout="centered")

SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
TZ = ZoneInfo("America/Sao_Paulo")

cookies = EncryptedCookieManager(
    prefix="yv_estoque",
    password=st.secrets.get("COOKIE_PASSWORD", "troque_isto_nos_secrets"),
)
if not cookies.ready():
    st.stop()


def now_local_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


@st.cache_resource
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


def _apierror_details(e: Exception) -> str:
    # Mostra o maximo possivel sem expor secrets
    if isinstance(e, APIError):
        try:
            status = getattr(e.response, "status_code", None)
            text = getattr(e.response, "text", "")
            return f"APIError status={status} body={text[:500]}"
        except Exception:
            return "APIError (sem detalhes)"
    return f"{type(e).__name__}: {str(e)[:500]}"


def with_retry(fn, *, tries=5, base_sleep=0.6):
    last = None
    for i in range(tries):
        try:
            return fn()
        except APIError as e:
            last = e
            # backoff: 0.6, 1.2, 2.4, 4.8...
            time.sleep(base_sleep * (2 ** i))
        except Exception as e:
            last = e
            time.sleep(base_sleep * (2 ** i))
    raise last


@st.cache_resource
def open_sheet():
    def _open():
        return gs_client().open_by_key(SPREADSHEET_ID)
    return with_retry(_open)


def read_sheet_df(sheet_name: str) -> pd.DataFrame:
    sh = open_sheet()

    def _read():
        ws = sh.worksheet(sheet_name)
        return pd.DataFrame(ws.get_all_records())

    return with_retry(_read)


def append_row(sheet_name: str, row: dict):
    sh = open_sheet()

    def _append():
        ws = sh.worksheet(sheet_name)
        headers = ws.row_values(1)
        values = [normalize_cell(row.get(h, "")) for h in headers]
        ws.append_row(values, value_input_option="USER_ENTERED")
        return True

    return with_retry(_append)


def is_active_flag(x) -> bool:
    return str(x).strip().lower() in ["1", "true", "sim", "yes", "y"]


def calc_saldos(trans_df: pd.DataFrame) -> pd.DataFrame:
    if trans_df is None or trans_df.empty:
        return pd.DataFrame(columns=["item_id", "saldo_atual"])

    df = trans_df.copy()
    if "quantidade_efetiva" not in df.columns:
        df["quantidade_efetiva"] = 0

    df["item_id"] = df["item_id"].astype(str)
    df["quantidade_efetiva"] = pd.to_numeric(
        df["quantidade_efetiva"], errors="coerce"
    ).fillna(0.0)

    out = df.groupby("item_id", as_index=False)["quantidade_efetiva"].sum()
    out = out.rename(columns={"quantidade_efetiva": "saldo_atual"})
    return out


def get_item(itens_df: pd.DataFrame, item_id: str):
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
    try:
        st.toast(msg, icon="✅")
    except Exception:
        st.success(msg)


def clear_item_and_ready_next():
    try:
        st.query_params.clear()
    except Exception:
        pass
    st.session_state["typed_item_id"] = ""
    st.session_state["qty"] = 1.0
    st.rerun()


# -------------------------
# Login persistente
# -------------------------
cookie_user_id = cookies.get("user_id")
cookie_user_nome = cookies.get("user_nome")

if cookie_user_id and "user_id" not in st.session_state:
    st.session_state["user_id"] = str(cookie_user_id)
    st.session_state["user_nome"] = str(cookie_user_nome or "")

st.session_state.setdefault("mode", "ENTRADA")
st.session_state.setdefault("typed_item_id", "")
st.session_state.setdefault("qty", 1.0)

# -------------------------
# Carrega usuarios
# -------------------------
try:
    users_df = read_sheet_df("USUARIOS")
except Exception as e:
    st.error("Falha ao ler USUARIOS.")
    st.code(_apierror_details(e))
    st.stop()

if users_df is None or users_df.empty:
    st.error("Aba USUARIOS vazia ou nao encontrada.")
    st.stop()

if "ativo" in users_df.columns:
    users_df["ativo_norm"] = users_df["ativo"].apply(is_active_flag)
else:
    users_df["ativo_norm"] = True

users_active = users_df[users_df["ativo_norm"]].copy()

def user_is_active(user_id: str) -> bool:
    if users_active.empty or "user_id" not in users_active.columns:
        return False
    return (users_active["user_id"].astype(str) == str(user_id)).any()

if "user_id" in st.session_state and not user_is_active(st.session_state["user_id"]):
    for k in ["user_id", "user_nome"]:
        st.session_state.pop(k, None)
    cookies["user_id"] = ""
    cookies["user_nome"] = ""
    cookies.save()

# -------------------------
# Login (so se nao logado)
# -------------------------
if "user_id" not in st.session_state:
    st.title("Estoque")
    st.caption("Login rápido")

    nomes = users_active["nome"].astype(str).tolist()
    nome = st.selectbox("Usuário", nomes)
    pin = st.text_input("PIN", type="password")

    if st.button("Entrar", use_container_width=True):
        u = users_active[users_active["nome"].astype(str) == str(nome)].iloc[0]
        if str(pin).strip() == str(u.get("pin", "")).strip():
            st.session_state["user_id"] = str(u.get("user_id", nome))
            st.session_state["user_nome"] = str(u.get("nome", nome))

            cookies["user_id"] = st.session_state["user_id"]
            cookies["user_nome"] = st.session_state["user_nome"]
            cookies.save()

            toast_ok("Logado")
            st.rerun()
        else:
            st.error("PIN incorreto")
    st.stop()

# -------------------------
# Header minimalista
# -------------------------
top = st.columns([3, 1])
with top[0]:
    st.caption(f"Logado: {st.session_state.get('user_nome','')}")
with top[1]:
    if st.button("Sair", use_container_width=True):
        for k in ["user_id", "user_nome"]:
            st.session_state.pop(k, None)
        cookies["user_id"] = ""
        cookies["user_nome"] = ""
        cookies.save()
        st.rerun()

st.divider()

# -------------------------
# Seletor de modo (direto)
# -------------------------
m1, m2, m3 = st.columns(3)
with m1:
    if st.button("Entrada", use_container_width=True):
        st.session_state["mode"] = "ENTRADA"
with m2:
    if st.button("Saída", use_container_width=True):
        st.session_state["mode"] = "SAIDA"
with m3:
    if st.button("Inventário", use_container_width=True):
        st.session_state["mode"] = "INVENTARIO"

st.caption(f"Modo: {st.session_state['mode']}")
st.divider()

# -------------------------
# Item: QR ou digitar
# -------------------------
qp = st.query_params
param_item = qp.get("item", None)

typed = st.text_input(
    "Item (QR ou digite o ID)",
    key="typed_item_id",
    placeholder="Ex: PR001",
)

if param_item:
    item_id = str(param_item).strip()
else:
    item_id = str(typed).strip()

go = st.columns([1, 1])
with go[0]:
    if st.button("Carregar", use_container_width=True):
        if item_id:
            st.query_params["item"] = item_id
            st.rerun()
        else:
            st.warning("Informe um item_id ou escaneie um QR.")
with go[1]:
    if st.button("Limpar", use_container_width=True):
        st.query_params.clear()
        st.session_state["typed_item_id"] = ""
        st.rerun()

if not item_id:
    st.info("Escaneie o QR do item ou digite o ID, depois toque em Carregar.")
    st.stop()

# -------------------------
# Carrega ITENS e TRANSACOES (com retry)
# -------------------------
try:
    itens_df = read_sheet_df("ITENS")
    trans_df = read_sheet_df("TRANSACOES")
except Exception as e:
    st.error("Falha ao acessar a planilha. Pode ser limite temporário do Google.")
    st.code(_apierror_details(e))
    st.stop()

saldos_df = calc_saldos(trans_df)

item = get_item(itens_df, item_id)
if item is None:
    st.error(f"Item não encontrado: {item_id}")
    st.stop()

nome_item = str(item.get("nome", item_id))
unidade = str(item.get("unidade", ""))
saldo_atual = get_saldo(saldos_df, item_id)

# -------------------------
# Tela do item (limpa)
# -------------------------
st.subheader(nome_item)
st.caption(f"ID: {item_id}  |  Saldo: {saldo_atual:g}  |  Und: {unidade}")

qtd = st.number_input("Quantidade", min_value=0.0, step=1.0, key="qty")

needs_confirm = True
if st.session_state["mode"] == "SAIDA":
    projected = float(saldo_atual) - float(qtd)
    if projected < 0:
        st.warning(f"Vai ficar negativo (proj: {projected:g}).")
        needs_confirm = st.checkbox("Confirmar mesmo assim")

btn_label = {
    "ENTRADA": "Confirmar entrada",
    "SAIDA": "Confirmar saída",
    "INVENTARIO": "Confirmar contagem",
}[st.session_state["mode"]]

if st.button(btn_label, use_container_width=True):
    qtd_f = float(qtd)

    if st.session_state["mode"] in ["ENTRADA", "SAIDA"] and qtd_f <= 0:
        st.error("Quantidade precisa ser maior que zero.")
        st.stop()

    if st.session_state["mode"] == "SAIDA":
        projected = float(saldo_atual) - float(qtd_f)
        if projected < 0 and not needs_confirm:
            st.error("Marque a confirmação para permitir saldo negativo.")
            st.stop()

    # Inventário: contagem -> ajuste automático (se tiver diferença)
    if st.session_state["mode"] == "INVENTARIO":
        saldo_teorico = float(saldo_atual)
        contado = float(qtd_f)
        diferenca = float(contado - saldo_teorico)

        # tenta registrar contagem (se aba existir)
        try:
            cont_row = {
                "contagem_id": str(uuid.uuid4()),
                "timestamp": now_local_iso(),
                "item_id": str(item_id),
                "saldo_teorico_no_momento": float(saldo_teorico),
                "quantidade_contada": float(contado),
                "diferenca": float(diferenca),
                "user_id": str(st.session_state.get("user_id", "")),
            }
            append_row("CONTAGENS", cont_row)
        except Exception:
            pass

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
                "obs": f"Ajuste inventário. Contado {contado:g}, teórico {saldo_teorico:g}.",
            }
            append_row("TRANSACOES", trans_row)

        toast_ok("Registrado")
        clear_item_and_ready_next()

    # Entrada / Saída
    else:
        if st.session_state["mode"] == "ENTRADA":
            acao = "ENTRADA"
            sinal_store = 1
            efetiva = float(qtd_f)
        else:
            acao = "SAIDA"
            sinal_store = -1
            efetiva = -float(qtd_f)

        row = {
            "trans_id": str(uuid.uuid4()),
            "timestamp": now_local_iso(),
            "item_id": str(item_id),
            "acao": str(acao),
            "sinal": int(sinal_store),
            "quantidade": float(qtd_f),
            "quantidade_efetiva": float(efetiva),
            "user_id": str(st.session_state.get("user_id", "")),
            "obs": "",
        }

        try:
            append_row("TRANSACOES", row)
        except Exception as e:
            st.error("Falha ao gravar na planilha (possível limite temporário). Tente novamente em alguns segundos.")
            st.code(_apierror_details(e))
            st.stop()

        toast_ok("Registrado")
        clear_item_and_ready_next()
