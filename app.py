import csv
import hashlib
import inspect
import hmac
import io
import json
import math
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="熱中症ビンゴ", page_icon="🥵", layout="centered")

# Streamlitのバージョン差を吸収する（ボタンを横いっぱいに広げる指定）
STRETCH = (
    {"width": "stretch"}
    if "width" in inspect.signature(st.button).parameters
    else {"use_container_width": True}
)
STRETCH_DL = (
    {"width": "stretch"}
    if "width" in inspect.signature(st.download_button).parameters
    else {"use_container_width": True}
)

# ------------------------------------------------------------
# スマホ向けの見た目
#  - 上のバー（Streamlitのヘッダー）に隠れないよう、上の余白を広げる
#  - 3×3のマスを、スマホでも横3列のまま表示する
# ------------------------------------------------------------
st.markdown(
    """
<style>
.block-container{padding:4.5rem .6rem 3rem!important}
[class*="st-key-brow"] [data-testid="stHorizontalBlock"]{flex-direction:row!important;flex-wrap:nowrap!important;gap:.35rem!important}
[class*="st-key-brow"] [data-testid="stColumn"],[class*="st-key-brow"] [data-testid="column"]{min-width:0!important;width:33%!important;flex:1 1 0!important}
[class*="st-key-brow"] .stButton>button{min-height:5.6rem;padding:.25rem .3rem;line-height:1.3}
[class*="st-key-brow"] .stButton>button p{font-size:.88rem;word-break:break-all;white-space:normal}
[class*="st-key-rolebtn"] .stButton>button{min-height:7rem;font-size:1.4rem}
</style>
""",
    unsafe_allow_html=True,
)

# ------------------------------------------------------------
# 定数
# ------------------------------------------------------------
N_CHILD = 6  # 子どものお題の数
N_ADULT = 7  # 大人のお題の数
MAX_LEN = 60  # お題1つの最大文字数
CHILD = "子ども"
ADULT = "大人"
JST = timezone(timedelta(hours=9))

# お題がまだ設定されていないときに使う「見本」
DEFAULT_TOPICS = {
    "c": (
        "太陽の下、帽子をかぶらずに遊んだ人",
        "水筒が空になって困った人",
        "プールの後にフラフラした人",
        "運動会の練習で頭が痛くなった人",
        "車の中で待たされて暑かった人",
        "公園のすべり台が熱くてびっくりした人",
    ),
    "a": (
        "炎天下の行列に、日傘なしで30分以上並んだ人",
        "締め切った車内にいて、頭がボーッとなった人",
        "エアコンをつけずに寝て、夜中に頭が痛くなった人",
        "夏フェスで水を買い忘れてフラフラになった人",
        "水分を取らずに炎天下でBBQを楽しんだ人",
        "猛暑日に自転車通勤して、着いた時点でヘトヘトだった人",
        "サウナの後に水分補給を忘れて立ちくらみした人",
    ),
}
CORNERS = [0, 2, 6, 8]  # 四隅 = 子どものお題
CROSS = [1, 3, 4, 5, 7]  # 十字 = 大人のお題
LINES = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
]
# (キー, ラベル, 複数行かどうか)
FIELDS_CELL = [
    ("who", "👤 話した人（ニックネームでもOK）", False),
    ("issue", "⚠️ 課題", True),
    ("solution", "💡 解決策", True),
    ("thoughts", "💬 感想", True),
]
FIELDS_OWN = [
    ("issue", "⚠️ 課題", True),
    ("solution", "💡 解決策", True),
]

# カードの組み合わせ数：四隅 6×5×4×3=360通り × 十字 7×6×5×4×3=2520通り = 907,200通り
N_CORNER_PERM = 6 * 5 * 4 * 3
N_CROSS_PERM = 7 * 6 * 5 * 4 * 3
N_CARDS = N_CORNER_PERM * N_CROSS_PERM
SPREAD = 100003  # カード番号を、見た目が大きく違うカードに散らすための数（N_CARDSと互いに素）
assert math.gcd(SPREAD, N_CARDS) == 1


# ------------------------------------------------------------
# サーバー側の共有メモリ（アプリが再起動・スリープすると消えます）
# ------------------------------------------------------------
@st.cache_resource
def get_store():
    return {
        "lock": threading.Lock(),
        "local": 0,  # スプレッドシートが使えないときの仮の番号
        "admin_topics": None,  # スプレッドシートが無いときの、運営が保存したお題
        "sheet_retry_at": 0.0,
        "sheet_error": "",
    }


STORE = get_store()


def secret(name):
    try:
        return st.secrets[name]
    except Exception:
        return None


# ------------------------------------------------------------
# お題の読み込み
# ------------------------------------------------------------
def load_topics_file():
    """同じフォルダの topics.json があれば読む"""
    try:
        path = Path(__file__).with_name("topics.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        c = [str(x).strip() for x in data["child"] if str(x).strip()]
        a = [str(x).strip() for x in data["adult"] if str(x).strip()]
        if len(c) == N_CHILD and len(a) == N_ADULT:
            return {"c": tuple(c), "a": tuple(a)}
    except Exception:
        pass
    return None


def topics_version(t):
    """お題の内容から作る短い識別子（お題が変わると変わる）"""
    return hashlib.md5("|".join(t["c"] + t["a"]).encode("utf-8")).hexdigest()[:6]


# ------------------------------------------------------------
# スプレッドシート連携（gspread）
#   「カード」シート … カードを作るたびに1行ふえる（番号＝行番号−1）
#   「お題」シート   … 運営が保存したお題
# ------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _open_sheets():
    import gspread
    from google.oauth2.service_account import Credentials

    info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    book = gspread.authorize(creds).open_by_key(str(st.secrets["SPREADSHEET_ID"]))

    def ensure(title, header, cols):
        try:
            ws = book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=title, rows=1000, cols=cols)
        if not ws.row_values(1):
            ws.update(range_name="A1", values=[header])
        return ws

    cards = ensure("カード", ["番号", "日時", "役割", "お題版"], 4)
    topics = ensure("お題", ["種別", "お題"], 2)
    return cards, topics


def sheets():
    """(カード用, お題用) のワークシート。使えなければ None（30秒は再挑戦しない）"""
    if time.time() < STORE["sheet_retry_at"]:
        return None
    try:
        return _open_sheets()
    except Exception as e:
        STORE["sheet_error"] = f"{type(e).__name__}: {str(e)[:150]}"
        STORE["sheet_retry_at"] = time.time() + 30
        return None


@st.cache_data(ttl=20, show_spinner=False)
def read_topics_sheet():
    pair = sheets()
    if not pair:
        return None
    try:
        rows = pair[1].get_all_values()[1:]
        c = [r[1].strip() for r in rows if len(r) > 1 and r[0].strip() == CHILD and r[1].strip()]
        a = [r[1].strip() for r in rows if len(r) > 1 and r[0].strip() == ADULT and r[1].strip()]
        if len(c) == N_CHILD and len(a) == N_ADULT:
            return {"c": tuple(c), "a": tuple(a)}
    except Exception:
        pass
    return None


@st.cache_data(ttl=20, show_spinner=False)
def card_stats():
    pair = sheets()
    if not pair:
        return None
    try:
        roles = pair[0].col_values(3)[1:]
        return {"total": len(roles), "adult": roles.count(ADULT), "child": roles.count(CHILD)}
    except Exception:
        return None


def current_topics():
    """いま使うお題。スプレッドシート → 運営が保存したもの → topics.json → 見本 の順"""
    t = read_topics_sheet()
    if t:
        return t
    if STORE["admin_topics"]:
        return STORE["admin_topics"]
    return load_topics_file() or DEFAULT_TOPICS


def sheet_status():
    if not secret("SPREADSHEET_ID") or not secret("gcp_service_account"):
        return "未設定（Secretsに SPREADSHEET_ID と gcp_service_account を設定してください）"
    if sheets():
        return "接続OK"
    return f"接続できません（{STORE['sheet_error']}）"


def _write_number_later(ws, row, n):
    """カード番号を、あとからシートのA列に書く（参加の混雑をさまたげないよう、少し待つ）"""

    def run():
        time.sleep(15 + random.random() * 30)
        for i in range(5):
            try:
                ws.update(range_name=f"A{row}", values=[[n]])
                return
            except Exception:
                time.sleep(5 * (i + 1))

    threading.Thread(target=run, daemon=True).start()


def issue_number(role, version):
    """カード番号を発行する。(番号, スプレッドシートに記録できたか) を返す。
    スプレッドシートに1行追加すると、行番号が決まる。追加は1行ずつ順番に処理されるので、
    同時に押されても、番号は重ならない。"""
    pair = sheets()
    if pair:
        ws = pair[0]
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        for attempt in range(6):
            try:
                res = ws.append_row(["", now, role, version], value_input_option="RAW")
                m = re.search(r"!\$?[A-Za-z]+\$?(\d+)", res["updates"]["updatedRange"])
                n = int(m.group(1)) - 1
                if n >= 1:
                    _write_number_later(ws, n + 1, n)
                    return n, True
            except Exception as e:
                STORE["sheet_error"] = f"{type(e).__name__}: {str(e)[:150]}"
                time.sleep(0.8 * (attempt + 1) + random.random())  # 混み合っているときは少し待って再挑戦
    # スプレッドシートが使えないとき：仮の番号（9001から）
    with STORE["lock"]:
        STORE["local"] += 1
        return 9000 + STORE["local"], False


# ------------------------------------------------------------
# カードの作成（番号が決まれば、カードも決まる。番号が違えば、カードも必ず違う）
# ------------------------------------------------------------
def pick_ordered(index, size, k):
    """size個から順番つきでk個選ぶ組み合わせの、index番目を取り出す"""
    avail = list(range(size))
    out = []
    for _ in range(k):
        index, d = divmod(index, len(avail))
        out.append(avail.pop(d))
    return out, avail


def deal(n, role, topics):
    idx = (n * SPREAD) % N_CARDS  # nが違えばidxも必ず違う
    corner_i, cross_i = idx % N_CORNER_PERM, idx // N_CORNER_PERM
    c_pick, c_rest = pick_ordered(corner_i, N_CHILD, 4)
    a_pick, a_rest = pick_ordered(cross_i, N_ADULT, 5)
    cells = {}
    for pos, i in zip(CORNERS, c_pick):
        cells[pos] = topics["c"][i]
    for pos, i in zip(CROSS, a_pick):
        cells[pos] = topics["a"][i]
    # 自分のお題は、カードに選ばれなかった残りのお題から選ぶ
    rest = [topics["c"][i] for i in c_rest] if role == CHILD else [topics["a"][i] for i in a_rest]
    own = rest[n % len(rest)]
    return cells, own


# ------------------------------------------------------------
# 入力の整形
# ------------------------------------------------------------
def esc(s):
    """Markdownの記号（* _ ` ~ $ : など）が、表示を崩さないようにする"""
    return re.sub(r"([\\`*_~$\[\]<>:])", r"\\\1", str(s))


# ------------------------------------------------------------
# 状態を変える関数（ボタンなどから呼ばれる）
# ------------------------------------------------------------
def pick_role(role):
    st.session_state["pending_role"] = role


def create_card(role):
    topics = current_topics()
    version = topics_version(topics)
    n, recorded = issue_number(role, version)
    st.session_state.update(
        topics=topics, version=version, number=n, recorded=recorded, role=role,
        sel=None, bingo_count=0, confirm_clear=False, confirm_back=False, old_version=False,
    )


def ask_back():
    st.session_state["confirm_back"] = True


def cancel_back():
    st.session_state["confirm_back"] = False


def go_back():
    st.session_state.update(number=None, sel=None, confirm_back=False)
    try:
        st.query_params.clear()
    except Exception:
        pass


def ask_clear():
    st.session_state["confirm_clear"] = True


def cancel_clear():
    st.session_state["confirm_clear"] = False


def clear_marks():
    for key in list(st.session_state.keys()):
        if key.startswith("in_m_") or key.startswith("in_c_"):
            del st.session_state[key]
    st.session_state.update(sel=None, bingo_count=0, confirm_clear=False)


def has_memo(mk):
    return any(
        st.session_state.get(f"in_m_{mk}_{f}", "").strip() for f, _, _ in FIELDS_CELL
    )


def is_on(n, pos):
    return bool(st.session_state.get(f"in_c_{n}_{pos}", False))


def select_cell(pos):
    """マスをタップ → そのマスのメモ欄を開く（もう一度タップすると閉じる）"""
    st.session_state["sel"] = None if st.session_state["sel"] == pos else pos


def close_panel():
    st.session_state["sel"] = None


def memo_changed(mk, chk_key):
    """メモを書いたら、自動でチェックを付ける"""
    if has_memo(mk):
        st.session_state[chk_key] = True


# ---- 運営用 ----
def try_unlock():
    now = time.time()
    if now < st.session_state["lock_until"]:
        wait = int(st.session_state["lock_until"] - now) + 1
        st.session_state["admin_msg"] = ("error", f"入力を間違えすぎました。{wait}秒後にもう一度どうぞ")
        return
    expected = str(secret("HOST_PASSWORD") or "")
    given = str(st.session_state.get("in_pw", ""))
    if expected and hmac.compare_digest(given.encode(), expected.encode()):
        st.session_state.update(admin_ok=True, admin_msg=None, fails=0, in_pw="")
    else:
        time.sleep(1)
        st.session_state["fails"] += 1
        if st.session_state["fails"] >= 5:
            st.session_state.update(lock_until=now + 60, fails=0)
        st.session_state["admin_msg"] = ("error", "パスワードが違います")


def lock_admin():
    st.session_state.update(admin_ok=False, admin_msg=None, in_pw="")


def parse_lines(text):
    return [s.strip() for s in (text or "").splitlines() if s.strip()]


def validate_topics(c, a):
    if len(c) != N_CHILD or len(a) != N_ADULT:
        return f"子どもは{N_CHILD}行（今{len(c)}行）、大人は{N_ADULT}行（今{len(a)}行）で入力してください"
    too_long = [t for t in c + a if len(t) > MAX_LEN]
    if too_long:
        return f"1つのお題は{MAX_LEN}文字以内にしてください：「{too_long[0][:15]}…」"
    if len(set(c + a)) != len(c + a):
        return "同じお題が2回入っています。重ならないようにしてください"
    return None


def save_topics():
    if not st.session_state.get("admin_ok"):
        st.session_state["admin_msg"] = ("error", "運営用のパスワードを入力してください")
        return
    c = parse_lines(st.session_state.get("in_adm_c"))
    a = parse_lines(st.session_state.get("in_adm_a"))
    err = validate_topics(c, a)
    if err:
        st.session_state["admin_msg"] = ("error", err)
        return
    topics = {"c": tuple(c), "a": tuple(a)}
    pair = sheets()
    if pair:
        try:
            rows = [["種別", "お題"]] + [[CHILD, t] for t in c] + [[ADULT, t] for t in a]
            pair[1].clear()
            pair[1].update(range_name="A1", values=rows)
            read_topics_sheet.clear()
            where = "スプレッドシートの「お題」シート"
        except Exception as e:
            st.session_state["admin_msg"] = ("error", f"スプレッドシートに保存できませんでした：{type(e).__name__}")
            return
    else:
        STORE["admin_topics"] = topics
        where = "サーバーのメモリ（スプレッドシート未接続のため。再起動で消えます）"
    st.session_state["admin_msg"] = (
        "success",
        f"お題を保存しました（保存先：{where}）。これから作られるカードに反映されます",
    )


# ------------------------------------------------------------
# 表示用の部品
# ------------------------------------------------------------
def build_csv(n, cells, own):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["マス", "お題", "チェック", "話した人", "課題", "解決策", "感想"])
    for pos in range(9):
        mk = f"{n}_{pos}"
        g = lambda f: st.session_state.get(f"in_m_{mk}_{f}", "")
        w.writerow(
            [pos + 1, cells[pos], "○" if is_on(n, pos) else "", g("who"), g("issue"), g("solution"), g("thoughts")]
        )
    g = lambda f: st.session_state.get(f"in_m_{n}_own_{f}", "")
    w.writerow(["自分のお題", own, "", "", g("issue"), g("solution"), ""])
    return buf.getvalue().encode("utf-8-sig")  # Excelでも文字化けしない


def memo_panel(n, pos, title):
    mk = f"{n}_{pos}"
    ck = f"in_c_{n}_{pos}"
    with st.container(border=True):
        st.markdown(f"**📝 {esc(title)}**")
        st.checkbox("✅ このお題に当てはまる人と話した（チェック）", key=ck)
        for f, label, multi in FIELDS_CELL:
            key = f"in_m_{mk}_{f}"
            if multi:
                st.text_area(label, key=key, height=90, on_change=memo_changed, args=(mk, ck))
            else:
                st.text_input(label, key=key, on_change=memo_changed, args=(mk, ck))
        st.button("✕ 閉じる", on_click=close_panel, **STRETCH)


def render_settings(n, cells, own):
    with st.expander("⚙️ 設定"):
        st.download_button(
            "💾 チェックとメモを保存（CSV）",
            data=build_csv(n, cells, own),
            file_name=f"熱中症ビンゴ_No{n}.csv",
            mime="text/csv",
            **STRETCH_DL,
        )
        if st.session_state["confirm_clear"]:
            st.warning("チェックとメモを、すべて消します。よろしいですか？")
            c1, c2 = st.columns(2)
            c1.button("はい、消す", on_click=clear_marks, type="primary", **STRETCH)
            c2.button("やめる", on_click=cancel_clear, **STRETCH)
        else:
            st.button("🧹 チェックとメモを消す", on_click=ask_clear, **STRETCH)
        if st.session_state["confirm_back"]:
            st.warning(
                "最初の画面にもどり、大人・子どもを選び直します。新しいカードができると、"
                "今のカードのチェックとメモは見られなくなります（先にCSV保存がおすすめです）。"
            )
            c1, c2 = st.columns(2)
            c1.button("もどる", on_click=go_back, type="primary", **STRETCH)
            c2.button("やめる", on_click=cancel_back, **STRETCH)
        else:
            st.button("↩ 最初にもどる（大人・子どもを選び直す）", on_click=ask_back, **STRETCH)


def render_admin():
    with st.expander("🔒 運営用（お題の入力・カード数）"):
        if not secret("HOST_PASSWORD"):
            st.warning(
                "運営画面は無効です。Secretsに HOST_PASSWORD を設定すると使えます。"
                "（お題をファイルで固定したいときは topics.json を使います）"
            )
            return
        if not st.session_state["admin_ok"]:
            st.text_input("運営パスワード", type="password", key="in_pw")
            st.button("🔓 開く", on_click=try_unlock, **STRETCH)
        else:
            st.markdown("**📊 作られたカードの数**")
            st.caption(f"スプレッドシート：{sheet_status()}")
            stats = card_stats()
            if stats:
                m1, m2, m3 = st.columns(3)
                m1.metric("合計", stats["total"])
                m2.metric("大人", stats["adult"])
                m3.metric("子ども", stats["child"])
                st.caption("数字は20秒ごとに更新されます。")
            else:
                st.caption("数を読み取れませんでした（スプレッドシートが未接続か、混み合っています）。")
            st.divider()
            st.caption(
                f"お題を1行に1つずつ入力します。子どもは{N_CHILD}行、大人は{N_ADULT}行です。"
                f"1つは{MAX_LEN}文字以内、同じお題は重ねないでください。"
            )
            st.text_area(f"👦 子どものお題（{N_CHILD}個）", key="in_adm_c", height=180)
            st.text_area(f"🧑 大人のお題（{N_ADULT}個）", key="in_adm_a", height=210)
            st.button("💾 お題を保存", on_click=save_topics, type="primary", **STRETCH)
            st.button("🔒 運営画面を閉じる", on_click=lock_admin, **STRETCH)
        msg = st.session_state["admin_msg"]
        if msg:
            (st.error if msg[0] == "error" else st.success)(msg[1])


def sync_query(n, role, version):
    """カード番号・役割・チェックをURLに残す（リロードや画面ロックの後も復帰できる）"""
    try:
        bits = "".join("1" if is_on(n, p) else "0" for p in range(9))
        want = {"no": str(n), "role": "c" if role == CHILD else "a", "k": bits, "v": version}
        for k, v in want.items():
            if st.query_params.get(k) != v:
                st.query_params[k] = v
    except Exception:
        pass


# ------------------------------------------------------------
# 状態の準備
# ------------------------------------------------------------
# 画面を再描画しても入力欄の中身が消えないようにする（"in_"で始まるキー）
for _k in list(st.session_state.keys()):
    if _k.startswith("in_"):
        st.session_state[_k] = st.session_state[_k]

_defaults = {
    "number": None,
    "role": None,
    "topics": None,
    "version": "",
    "recorded": True,
    "old_version": False,
    "pending_role": None,
    "sel": None,
    "bingo_count": 0,
    "confirm_clear": False,
    "confirm_back": False,
    "admin_ok": False,
    "admin_msg": None,
    "fails": 0,
    "lock_until": 0.0,
}
for _k, _v in _defaults.items():
    st.session_state.setdefault(_k, _v)

# 最初の1回だけ、URLの ?no=…&role=…&k=… から復帰する（新しい番号は発行しない）
if "init_done" not in st.session_state:
    st.session_state["init_done"] = True
    _t = current_topics()
    st.session_state["in_adm_c"] = "\n".join(_t["c"])
    st.session_state["in_adm_a"] = "\n".join(_t["a"])
    _qp = st.query_params
    _no, _role = str(_qp.get("no", "")), {"a": ADULT, "c": CHILD}.get(_qp.get("role"))
    if re.fullmatch(r"[0-9]{1,6}", _no) and 1 <= int(_no) <= N_CARDS and _role:
        _ver = topics_version(_t)
        st.session_state.update(
            number=int(_no), role=_role, topics=_t, version=_ver,
            old_version=bool(_qp.get("v")) and _qp.get("v") != _ver,
        )
        _bits = str(_qp.get("k", ""))
        if re.fullmatch(r"[01]{9}", _bits):
            for _p, _b in enumerate(_bits):
                if _b == "1":
                    st.session_state[f"in_c_{_no}_{_p}"] = True

# ------------------------------------------------------------
# 画面
# ------------------------------------------------------------
st.markdown("### 🥵 熱中症ビンゴ")

# --- 役割が選ばれた直後：カードを作る ---
_pending = st.session_state["pending_role"]
if _pending:
    st.session_state["pending_role"] = None
    if st.session_state["number"] is None:
        with st.spinner("あなたのカードを作っています…"):
            create_card(_pending)
        st.rerun()

# --- 1枚目の画面：大人・子どもを選ぶ ---
if st.session_state["number"] is None:
    st.markdown("#### あなたはどちらですか？")
    st.caption("選ぶと、あなただけのビンゴカードができます。")
    c1, c2 = st.columns(2)
    with c1:
        with st.container(key="rolebtn_a"):
            st.button("🧑 大人", on_click=pick_role, args=(ADULT,), type="primary", **STRETCH)
    with c2:
        with st.container(key="rolebtn_c"):
            st.button("👦 子ども", on_click=pick_role, args=(CHILD,), type="primary", **STRETCH)
    render_admin()
    st.stop()

# --- 2枚目の画面：ビンゴカード ---
n = st.session_state["number"]
role = st.session_state["role"]
topics = st.session_state["topics"]
version = st.session_state["version"]
cells, own = deal(n, role, topics)

if st.session_state["old_version"]:
    st.warning("お題が入れ替わった可能性があります。カードの内容が、前と違うかもしれません。")

# --- 上：自分のお題とメモ ---
own_filled = any(
    st.session_state.get(f"in_m_{n}_own_{f}", "").strip() for f, _, _ in FIELDS_OWN
)
st.info(f"⭐ あなたのお題（{role}）：{esc(own)}")
if own_filled:
    st.caption("📝 自分のお題のメモを記入済みです")
with st.expander("📝 自分のお題のメモ（課題・解決策）"):
    for f, label, multi in FIELDS_OWN:
        st.text_area(label, key=f"in_m_{n}_own_{f}", height=90)

# --- ビンゴカード（マスをタップすると、その行の下にメモ欄が出る） ---
sel = st.session_state["sel"]
tag = "" if st.session_state["recorded"] else "（仮番号）"
st.caption(f"あなたのカード：**No.{n}**{tag}　マスをタップするとメモ欄が開きます")
for row in range(3):
    with st.container(key=f"brow{row}"):
        cols = st.columns(3)
        for k in range(3):
            pos = row * 3 + k
            on = is_on(n, pos)
            label = ("✅ " if on else "") + esc(cells[pos])
            if has_memo(f"{n}_{pos}"):
                label += " 📝"
            if sel == pos:
                label = "👉 " + label
            cols[k].button(
                label,
                key=f"cell_{pos}",
                type="primary" if on else "secondary",
                on_click=select_cell,
                args=(pos,),
                **STRETCH,
            )
    if sel is not None and sel // 3 == row:
        memo_panel(n, sel, cells[sel])

# ---- ビンゴ判定 ----
done = [l for l in LINES if all(is_on(n, p) for p in l)]
if len(done) > st.session_state["bingo_count"]:
    st.balloons()
st.session_state["bingo_count"] = len(done)
if done:
    st.success(f"🎉 {len(done)}ビンゴ！！")
st.caption("💧 ゲーム中もこまめに水分補給＆休憩を忘れずに！")

# ---- 設定・運営用 ----
render_settings(n, cells, own)
render_admin()
sync_query(n, role, version)
