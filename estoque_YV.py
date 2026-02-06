import streamlit as st
import pandas as pd
import uuid
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from gspread.exceptions import APIError, WorksheetNotFound
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


def with_retry(fn, *, tries=4, base_sleep=0.8):
    last = None
    for i in range(tries):
        try:
            return fn()
        except APIError as e:
            last = e
            # Em 429, esperar ajuda; em 403/404, nao adianta muito, mas ainda tenta pouco
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


def toast_ok(msg: str):
    try:
        st.toast(msg, icon="✅")
    except Exception:
        st.success(msg)


def is_active_flag(x) -> bool:
    return str(x).strip().lower() in ["1", "true", "sim", "yes", "y"]


# Caches longos para reduzir reads/min
@st.cache_data(ttl=600)  # 10 min
def read_users_df() -> pd.DataFrame:
    sh = open_sheet()
    def _read():
        ws = sh.worksheet("USUARIOS")
        return pd.DataFrame(ws.get_all_records())
    return with_retry(_read)


@st.cache_data(ttl=600)  # 10 min
def read_itens_df() -> pd.DataFrame:
    sh = open_sheet()
    def _read():
        ws = sh.worksheet("ITENS")
        return pd.DataFrame(ws.get_all_records())
    return with_retry(_read)


def get_item(itens_df: pd.DataFrame, item_id: str):
    if itens_df is None or itens_df.empty or "item_id" not in itens_df.columns:
        return None
    df = itens_df.copy()
    df["item_id"] = df["item_id"].astype(str)
    rows = df[df["item_id"] == str(item_id)]
    if rows.empty:
        return None
    return rows.iloc[0]


def append_row(sheet_name: str, row: dict):
    sh = open_sheet()
    def _append():
        ws = sh.worksheet(sheet_name)
        headers = ws.row_values(1)
        values = [normalize_cell(row.get(h, "")) for h in headers]
        ws.append_row(values, value_input_option="USER_ENTERED")
        return True
    return with_retry(_append)


# SALDOS: leitura por item (cache curto)
@st.cache_data(ttl=15)
def get_saldo_from_saldos(item_id: str) -> float:
    sh = open_sheet()

    def _get():
        ws = sh.worksheet("SALDOS")
        headers = ws.row_values(1)
        try:
            col_item = headers.index("item_id") + 1
        except ValueError:
            col_item = 1
        try:
            col_saldo = headers.index("saldo_atual") + 1
        except ValueError:
            col_saldo = 2

        # Busca item_id na coluna A (ou col_item)
        col_vals = ws.col_values(col_item)  # inclui header
        # otimiza: se for curto (50 itens), ok
        target = str(item_id).strip()
        for idx, v in enumerate(col_vals[1:], start=2):  # linhas começam em 2
            if str(v).strip() == target:
                raw = ws.cell(idx, col_saldo).value
                try:
                    return float(str(raw).replace(",", "."))
                except Exception:
                    return 0.0
        return 0.0

    return float(with_retry(_get))


def set_saldo_in_saldos(item_id: str, new_saldo: float):
    sh = open_sheet()

    def _set():
        ws = sh.worksheet("SALDOS")
        headers = ws.row_values(1)
        try:
            col_item = headers.index("item_id") + 1
        except ValueError:
            col_item = 1
        try:
            col_saldo = headers.index("saldo_atual") + 1
        except ValueError:
            col_saldo = 2

        col_vals = ws.col_values(col_item)
        target = str(item_id).strip()
        for idx, v in enumerate(col_vals[1:], start=2):
            if str(v).strip() == target:
                ws.update_cell(idx, col_saldo, float(new_saldo))
                return True

        # Se nao existe a linha do item, cria no final
        next_row = len(col_vals) + 1
        ws.update_cell(next_row, col_item, target)
        ws.update_cell(next_row, col_saldo, float(new_saldo))
        return True

    ok = with_retry(_set)
    # invalida cache curto do saldo
    try:
        get_saldo_from_saldos.clear()
    except Exception:
        pass
    return ok


def apply_delta(item_id: str, delta: float) -> float:
    # Le 1 saldo (cache curto), grava novo saldo
    saldo = get_saldo_from_saldos(item_id)
    novo = float(saldo) + float(delta)
    set_saldo_in_saldos(item_id, novo)
    return novo


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
# Carrega usuarios (cache longo)
# -------------------------
try:
    users_df = read_users_df()
except Exception as e:
    st.error("Falha ao ler USUARIOS (limite do Google Sheets).")
    st.write("Tente novamente em alguns segundos.")
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


# Se cookie existe mas usuario nao ativo, derruba login
if "user_id" in st.session_state and not user_is_active(st.session_state["user_id"]):
    st.session_state.pop("user_id", None)
    st.session_state.pop("user_nome", None)
    cookies["user_id"] = ""
    cookies["user_nome"] = ""
    cookies.save()

# -------------------------
# Login (só se nao logado)
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
        st.session_state.pop("user_id", None)
        st.session_state.pop("user_nome", None)
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
# Item: QR ou digitar (sem fricção)
# -------------------------
qp = st.query_params
param_item = qp.get("item", None)

typed = st.text_input(
    "Item",
    key="typed_item_id",
    placeholder="Escaneie o QR ou digite o ID (ex: PR001)",
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
    st.info("Escaneie o QR do item ou digite o ID e toque em Carregar.")
    st.stop()

# -------------------------
# Carrega ITENS (cache longo) e SALDO (barato)
# -------------------------
try:
    itens_df = read_itens_df()
except Exception:
    st.error("Falha ao ler ITENS (limite do Google Sheets). Tente novamente em alguns segundos.")
    st.stop()

item = get_item(itens_df, item_id)
if item is None:
    st.error(f"Item não encontrado: {item_id}")
    st.stop()

nome_item = str(item.get("nome", item_id))
unidade = str(item.get("unidade", ""))

# saldo vem de SALDOS (nao de TRANSACOES)
try:
    saldo_atual = float(get_saldo_from_saldos(item_id))
except Exception:
    saldo_atual = 0.0

st.subheader(nome_item)
st.caption(f"ID: {item_id}  |  Saldo: {saldo_atual:g}  |  Und: {unidade}")

qtd = st.number_input("Quantidade", min_value=0.0, step=1.0, key="qty")

# Confirmação discreta para saldo negativo
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

    # INVENTARIO: compara contagem vs SALDOS e gera AJUSTE automatico
    if st.session_state["mode"] == "INVENTARIO":
        saldo_teorico = float(saldo_atual)
        contado = float(qtd_f)
        diferenca = float(contado - saldo_teorico)

        # registra contagem (se quiser manter)
        try:
            append_row("CONTAGENS", {
                "contagem_id": str(uuid.uuid4()),
                "timestamp": now_local_iso(),
                "item_id": str(item_id),
                "saldo_teorico_no_momento": float(saldo_teorico),
                "quantidade_contada": float(contado),
                "diferenca": float(diferenca),
                "user_id": str(st.session_state.get("user_id", "")),
            })
        except Exception:
            pass

        # se diferenca, grava transacao AJUSTE e atualiza SALDOS
        if abs(diferenca) > 1e-9:
            sinal_store = 1 if diferenca > 0 else -1

            append_row("TRANSACOES", {
                "trans_id": str(uuid.uuid4()),
                "timestamp": now_local_iso(),
                "item_id": str(item_id),
                "acao": "AJUSTE",
                "sinal": int(sinal_store),
                "quantidade": float(abs(diferenca)),
                "quantidade_efetiva": float(diferenca),
                "user_id": str(st.session_state.get("user_id", "")),
                "obs": f"Ajuste inventário. Contado {contado:g}, teórico {saldo_teorico:g}.",
            })

            novo = apply_delta(item_id, float(diferenca))
        else:
            novo = saldo_teorico

        toast_ok("Registrado")
        clear_item_and_ready_next()

    # ENTRADA / SAIDA: grava transacao e atualiza SALDOS
    else:
        if st.session_state["mode"] == "ENTRADA":
            acao = "ENTRADA"
            delta = float(qtd_f)
            sinal_store = 1
        else:
            acao = "SAIDA"
            delta = -float(qtd_f)
            sinal_store = -1

        append_row("TRANSACOES", {
            "trans_id": str(uuid.uuid4()),
            "timestamp": now_local_iso(),
            "item_id": str(item_id),
            "acao": str(acao),
            "sinal": int(sinal_store),
            "quantidade": float(qtd_f),
            "quantidade_efetiva": float(delta),
            "user_id": str(st.session_state.get("user_id", "")),
            "obs": "",
        })

        novo = apply_delta(item_id, float(delta))

        toast_ok("Registrado")
        clear_item_and_ready_next()
