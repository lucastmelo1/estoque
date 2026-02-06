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

# --------------------
# Config
# --------------------
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


def with_retry(fn, *, tries=3, base_sleep=0.7):
    last = None
    for i in range(tries):
        try:
            return fn()
        except APIError as e:
            last = e
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

    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def toast_ok(msg: str):
    try:
        st.toast(msg, icon="✅")
    except Exception:
        st.success(msg)


def toast_warn(msg: str):
    try:
        st.toast(msg, icon="⚠️")
    except Exception:
        st.warning(msg)


def is_active_flag(x) -> bool:
    return str(x).strip().lower() in ["1", "true", "sim", "yes", "y"]


def is_manager_row(row: pd.Series) -> bool:
    # Aceita varias colunas para flexibilidade
    candidates = ["nivel", "perfil", "role", "gestor", "is_manager"]
    for c in candidates:
        if c in row.index:
            v = str(row.get(c, "")).strip().lower()
            if v in ["gestor", "admin", "administrador", "manager", "owner", "true", "1", "sim", "yes", "y"]:
                return True
    return False


@st.cache_data(ttl=600)
def read_users_df() -> pd.DataFrame:
    sh = open_sheet()
    def _read():
        ws = sh.worksheet("USUARIOS")
        return pd.DataFrame(ws.get_all_records())
    return with_retry(_read)


@st.cache_data(ttl=600)
def read_itens_df() -> pd.DataFrame:
    sh = open_sheet()
    def _read():
        ws = sh.worksheet("ITENS")
        return pd.DataFrame(ws.get_all_records())
    return with_retry(_read)


@st.cache_data(ttl=30)
def read_saldos_df() -> pd.DataFrame:
    sh = open_sheet()
    def _read():
        ws = sh.worksheet("SALDOS")
        df = pd.DataFrame(ws.get_all_records())
        if df is None or df.empty:
            return pd.DataFrame(columns=["item_id", "saldo_atual"])
        if "item_id" not in df.columns:
            df["item_id"] = ""
        if "saldo_atual" not in df.columns:
            df["saldo_atual"] = 0
        df["item_id"] = df["item_id"].astype(str).str.strip().str.upper()
        df["saldo_atual"] = pd.to_numeric(df["saldo_atual"], errors="coerce").fillna(0.0)
        return df[["item_id", "saldo_atual"]].copy()
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


def get_item(itens_df: pd.DataFrame, item_id: str):
    if itens_df is None or itens_df.empty or "item_id" not in itens_df.columns:
        return None
    df = itens_df.copy()
    df["item_id"] = df["item_id"].astype(str).str.strip().str.upper()
    rows = df[df["item_id"] == str(item_id).strip().upper()]
    if rows.empty:
        return None
    return rows.iloc[0]


def get_saldo_cached(item_id: str) -> float:
    df = read_saldos_df()
    if df is None or df.empty:
        return 0.0
    item_id = str(item_id).strip().upper()
    r = df[df["item_id"] == item_id]
    if r.empty:
        return 0.0
    return float(r["saldo_atual"].iloc[0])


def set_saldo_in_saldos(item_id: str, new_saldo: float):
    sh = open_sheet()
    item_id = str(item_id).strip().upper()

    def _set():
        ws = sh.worksheet("SALDOS")
        headers = ws.row_values(1)
        col_item = (headers.index("item_id") + 1) if "item_id" in headers else 1
        col_saldo = (headers.index("saldo_atual") + 1) if "saldo_atual" in headers else 2

        col_vals = ws.col_values(col_item)
        for idx, v in enumerate(col_vals[1:], start=2):
            if str(v).strip().upper() == item_id:
                ws.update_cell(idx, col_saldo, float(new_saldo))
                return True

        # cria linha se nao existir
        next_row = len(col_vals) + 1
        ws.update_cell(next_row, col_item, item_id)
        ws.update_cell(next_row, col_saldo, float(new_saldo))
        return True

    ok = with_retry(_set)
    try:
        read_saldos_df.clear()
    except Exception:
        pass
    return ok


def apply_delta(item_id: str, delta: float) -> float:
    saldo = get_saldo_cached(item_id)
    novo = float(saldo) + float(delta)
    set_saldo_in_saldos(item_id, novo)
    return novo


def reset_for_next_item():
    # nao mexer no state do widget ja criado, trocar chaves
    st.query_params.clear()
    st.session_state["item_key"] = int(st.session_state.get("item_key", 0)) + 1
    st.session_state["qty_key"] = int(st.session_state.get("qty_key", 0)) + 1
    st.rerun()


# --------------------
# UI Styles (mobile friendly)
# --------------------
st.markdown(
    """
<style>
:root{
  --bg: #FAF5E6;
  --card: #FFFFFF;
  --ink: #1B2A41;
  --muted: #4A4A4A;
  --soft: #EFE6D5;
  --soft-border: #DED0B6;
  --ok: #1B2A41;
  --danger: #9A2C2C;
  --shadow: rgba(27,42,65,0.12);
}

html, body, [class*="stApp"]{
  background: var(--bg) !important;
  color: var(--ink);
}

.block-container{
  padding-top: 1rem;
  padding-bottom: 2rem;
  max-width: 720px;
}

.yv-card{
  background: var(--card);
  border-radius: 16px;
  box-shadow: 0 10px 25px var(--shadow);
  padding: 16px 16px;
  border: 1px solid rgba(222,208,182,0.55);
}

.yv-title{
  font-weight: 800;
  letter-spacing: 0.5px;
  margin: 0;
  color: var(--ink);
}

.yv-sub{
  color: var(--muted);
  margin-top: 6px;
  margin-bottom: 0;
  font-size: 0.95rem;
}

.yv-chip{
  display:inline-block;
  background: var(--soft);
  border: 1px solid var(--soft-border);
  border-radius: 10px;
  padding: 8px 10px;
  font-weight: 650;
  color: var(--ink);
}

.yv-seg .stRadio > div{
  background: var(--card);
  border-radius: 14px;
  border: 1px solid rgba(222,208,182,0.7);
  padding: 8px 10px;
  box-shadow: 0 8px 18px rgba(27,42,65,0.06);
}
.yv-seg label{
  font-weight: 700;
}
.yv-seg [role="radiogroup"]{
  display:flex;
  gap:10px;
  justify-content:space-between;
}
.yv-seg [role="radio"]{
  flex: 1 1 0;
  border-radius: 12px;
  padding: 10px 10px;
  border: 1px solid rgba(222,208,182,0.65);
  background: #fff;
}
.yv-seg [role="radio"][aria-checked="true"]{
  background: var(--ink);
  border-color: var(--ink);
  color: #fff;
}
.yv-seg [role="radio"][aria-checked="true"] *{
  color: #fff !important;
}

button[kind="primary"]{
  border-radius: 12px !important;
  font-weight: 800 !important;
  padding: 0.85rem 1rem !important;
  box-shadow: 0 8px 18px rgba(27,42,65,0.18) !important;
}

@media (max-width: 480px){
  .block-container{ padding-left: 0.9rem; padding-right: 0.9rem; }
  .yv-title{ font-size: 1.65rem; }
}
</style>
""",
    unsafe_allow_html=True,
)

# --------------------
# Base state
# --------------------
st.session_state.setdefault("mode", "ENTRADA")
st.session_state.setdefault("item_key", 0)
st.session_state.setdefault("qty_key", 0)

# --------------------
# Login persistente
# --------------------
cookie_user_id = cookies.get("user_id")
cookie_user_nome = cookies.get("user_nome")
if cookie_user_id and "user_id" not in st.session_state:
    st.session_state["user_id"] = str(cookie_user_id)
    st.session_state["user_nome"] = str(cookie_user_nome or "")

# --------------------
# Load users
# --------------------
users_df = read_users_df()
if "ativo" in users_df.columns:
    users_df["ativo_norm"] = users_df["ativo"].apply(is_active_flag)
else:
    users_df["ativo_norm"] = True
users_active = users_df[users_df["ativo_norm"]].copy()

def user_row_by_name(name: str):
    r = users_active[users_active["nome"].astype(str) == str(name)]
    if r.empty:
        return None
    return r.iloc[0]

def user_row_by_id(user_id: str):
    if "user_id" not in users_active.columns:
        return None
    r = users_active[users_active["user_id"].astype(str) == str(user_id)]
    if r.empty:
        return None
    return r.iloc[0]

if "user_id" in st.session_state:
    urow = user_row_by_id(st.session_state["user_id"])
    if urow is None:
        st.session_state.pop("user_id", None)
        st.session_state.pop("user_nome", None)
        cookies["user_id"] = ""
        cookies["user_nome"] = ""
        cookies.save()

# --------------------
# Login screen
# --------------------
if "user_id" not in st.session_state:
    st.markdown('<div class="yv-card">', unsafe_allow_html=True)
    st.markdown('<h1 class="yv-title">Estoque</h1>', unsafe_allow_html=True)
    st.markdown('<p class="yv-sub">Login rápido</p>', unsafe_allow_html=True)

    nomes = users_active["nome"].astype(str).tolist()
    nome = st.selectbox("Usuário", nomes)
    pin = st.text_input("PIN", type="password")

    if st.button("Entrar", type="primary", use_container_width=True):
        u = user_row_by_name(nome)
        if u is not None and str(pin).strip() == str(u.get("pin", "")).strip():
            st.session_state["user_id"] = str(u.get("user_id", nome))
            st.session_state["user_nome"] = str(u.get("nome", nome))

            cookies["user_id"] = st.session_state["user_id"]
            cookies["user_nome"] = st.session_state["user_nome"]
            cookies.save()

            toast_ok("Logado")
            st.rerun()
        else:
            st.error("PIN incorreto")

    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()

# --------------------
# Sidebar (consulta saldo, restrita a gestor)
# --------------------
urow = user_row_by_id(st.session_state["user_id"])
is_manager = bool(is_manager_row(urow)) if urow is not None else False

with st.sidebar:
    st.markdown("### Consulta de saldo")
    if is_manager:
        itens_df_for_side = read_itens_df()
        q = st.text_input("Buscar item (ID ou nome)", placeholder="Ex: PR001 ou File mignon")

        if q:
            q_norm = str(q).strip().upper()
            df = itens_df_for_side.copy()
            df["item_id"] = df["item_id"].astype(str).str.strip().str.upper()
            df["nome"] = df.get("nome", "").astype(str)
            hits = df[
                df["item_id"].str.contains(q_norm, na=False)
                | df["nome"].str.upper().str.contains(q_norm, na=False)
            ].head(10)

            if hits.empty:
                st.info("Nada encontrado.")
            else:
                for _, r in hits.iterrows():
                    iid = str(r["item_id"]).strip().upper()
                    nm = str(r.get("nome", iid))
                    s = get_saldo_cached(iid)
                    st.write(f"**{iid}**  |  {nm}")
                    st.write(f"Saldo: **{s:g}**")
                    st.divider()
        else:
            st.caption("Digite para buscar e ver saldo.")
    else:
        st.info("Acesso restrito ao nível gestor.")

# --------------------
# Header minimalista
# --------------------
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

# --------------------
# Mode selector with highlight (segmented)
# --------------------
st.markdown('<div class="yv-seg">', unsafe_allow_html=True)
mode = st.radio(
    "Modo",
    options=["ENTRADA", "SAIDA", "INVENTARIO"],
    horizontal=True,
    index=["ENTRADA", "SAIDA", "INVENTARIO"].index(st.session_state.get("mode", "ENTRADA")),
    label_visibility="collapsed",
)
st.markdown("</div>", unsafe_allow_html=True)
st.session_state["mode"] = mode

st.markdown("<br>", unsafe_allow_html=True)

# --------------------
# Item input: no "Carregar" button
# - If QR uses ?item=PR001, auto opens item
# - If typed, on_change sets query param and reruns
# --------------------
qp = st.query_params
param_item = qp.get("item", None)

def normalize_item_id(x: str) -> str:
    return str(x or "").strip().upper()

def on_item_change(key: str):
    raw = st.session_state.get(key, "")
    item_norm = normalize_item_id(raw)
    if item_norm:
        st.query_params["item"] = item_norm
        st.rerun()

item_input_key = f"item_input_{st.session_state['item_key']}"

# If arrived via QR, we keep param and show a compact chip, plus an optional quick change field
if param_item:
    item_id = normalize_item_id(param_item)
    st.markdown('<div class="yv-card">', unsafe_allow_html=True)
    st.markdown(f'<span class="yv-chip">Item: {item_id}</span>', unsafe_allow_html=True)
    st.markdown('<p class="yv-sub">Para trocar, digite outro ID e pressione Enter</p>', unsafe_allow_html=True)

    st.text_input(
        "Trocar item",
        key=item_input_key,
        placeholder="Digite novo ID (ex: PR002) e pressione Enter",
        on_change=on_item_change,
        args=(item_input_key,),
        label_visibility="collapsed",
    )
    st.markdown("</div>", unsafe_allow_html=True)
else:
    st.markdown('<div class="yv-card">', unsafe_allow_html=True)
    st.markdown('<h2 class="yv-title" style="font-size:1.35rem;">Escanear ou digitar item</h2>', unsafe_allow_html=True)
    st.markdown('<p class="yv-sub">Digite o ID e pressione Enter</p>', unsafe_allow_html=True)

    st.text_input(
        "Item",
        key=item_input_key,
        placeholder="Ex: PR001",
        on_change=on_item_change,
        args=(item_input_key,),
        label_visibility="collapsed",
    )

    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()

# --------------------
# Load item + saldo
# --------------------
itens_df = read_itens_df()
item = get_item(itens_df, item_id)
if item is None:
    st.error(f"Item não encontrado: {item_id}")
    st.caption("Verifique o ID ou tente novamente.")
    st.stop()

nome_item = str(item.get("nome", item_id))
unidade = str(item.get("unidade", ""))
saldo_atual = float(get_saldo_cached(item_id))

# --------------------
# Main card
# --------------------
st.markdown("<br>", unsafe_allow_html=True)
st.markdown('<div class="yv-card">', unsafe_allow_html=True)

st.markdown(f'<h1 class="yv-title">{nome_item}</h1>', unsafe_allow_html=True)
st.markdown(
    f'<p class="yv-sub">ID: <b>{item_id}</b> &nbsp; | &nbsp; Und: <b>{unidade}</b> &nbsp; | &nbsp; Saldo: <b>{saldo_atual:g}</b></p>',
    unsafe_allow_html=True,
)

qty_input_key = f"qty_{st.session_state['qty_key']}"
qtd = st.number_input(
    "Quantidade",
    min_value=0.0,
    step=1.0,
    value=1.0,
    key=qty_input_key,
)

needs_confirm = True
if st.session_state["mode"] == "SAIDA":
    projected = float(saldo_atual) - float(qtd)
    if projected < 0:
        st.markdown(
            f"<p style='color: var(--danger); font-weight:800;'>Atenção: saldo negativo projetado ({projected:g})</p>",
            unsafe_allow_html=True,
        )
        needs_confirm = st.checkbox("Confirmar mesmo assim")

btn_label = {
    "ENTRADA": "Confirmar entrada",
    "SAIDA": "Confirmar saída",
    "INVENTARIO": "Confirmar contagem",
}[st.session_state["mode"]]

if st.button(btn_label, type="primary", use_container_width=True):
    qtd_f = float(qtd)

    if st.session_state["mode"] in ["ENTRADA", "SAIDA"] and qtd_f <= 0:
        st.error("Quantidade precisa ser maior que zero.")
        st.stop()

    if st.session_state["mode"] == "SAIDA":
        projected = float(saldo_atual) - float(qtd_f)
        if projected < 0 and not needs_confirm:
            st.error("Marque a confirmação para permitir saldo negativo.")
            st.stop()

    # INVENTARIO: compara com SALDOS e ajusta
    if st.session_state["mode"] == "INVENTARIO":
        saldo_teorico = float(saldo_atual)
        contado = float(qtd_f)
        diferenca = float(contado - saldo_teorico)

        # contagem (opcional)
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
                "obs": f"Ajuste inventário. Contado {contado:g}, teorico {saldo_teorico:g}.",
            })

            apply_delta(item_id, float(diferenca))

        toast_ok("Registrado")
        reset_for_next_item()

    # ENTRADA / SAIDA
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

        apply_delta(item_id, float(delta))

        toast_ok("Registrado")
        reset_for_next_item()

st.markdown("</div>", unsafe_allow_html=True)

# Quick actions
c1, c2 = st.columns(2)
with c1:
    if st.button("Limpar item", use_container_width=True):
        reset_for_next_item()
with c2:
    st.caption("Dica: use QR com ?item=PR001 para abrir direto.")
