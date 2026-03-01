from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import threading
import time
from queue import Queue

from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_file, abort

from wordcloud import WordCloud
import qrcode

from collections import Counter

# =======================
# 환경설정
# =======================
APP_HOST = "0.0.0.0"
APP_PORT = int(os.getenv("PORT", "5000"))

# Render 무료 플랜에서 쓰기 가능한 안전 경로: /tmp
# (주의: 서버 재시작 시 DB 초기화될 수 있음)
DB_PATH = os.getenv("DB_PATH", "/tmp/responses.db")

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # 예: https://xxxxx.onrender.com
TEACHER_TOKEN = os.getenv("TEACHER_TOKEN", "change-me")

# 한글 폰트(필요하면 사용): 프로젝트 폴더에 ttf 넣고 환경변수로 파일명 지정
FONT_PATH = os.getenv("WC_FONT_PATH", "NanumGothic.ttf")  # 예: "NanumGothic.ttf"

KOREAN_STOPWORDS = {
    "그리고", "근데", "그래서", "하지만", "또는", "저는", "제가", "그냥",
    "진짜", "너무", "조금", "정말", "같아요", "합니다", "하는", "에서", "으로",
    "은", "는", "이", "가", "을", "를", "에", "의", "과", "와", "도", "만",
    "한", "하다", "되다", "있다", "없다",
}


app = Flask(__name__)

# SSE 구독자 큐
SUBSCRIBERS: list[Queue] = []
SUB_LOCK = threading.Lock()


# =======================
# DB (중요: 연결할 때마다 테이블 보장 + timeout)
# =======================
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row

    # 락 완화(가능한 경우)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=10000;")  # ms
    except Exception:
        pass

    # ✅ 핵심: 어떤 요청이 먼저 와도 테이블 항상 보장
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def add_response(text: str) -> None:
    conn = db_conn()
    try:
        conn.execute("INSERT INTO responses(text, created_at) VALUES (?, ?)", (text, time.time()))
        conn.commit()
    finally:
        conn.close()


def clear_responses() -> None:
    conn = db_conn()
    try:
        conn.execute("DELETE FROM responses")
        conn.commit()
    finally:
        conn.close()


def get_all_text() -> str:
    conn = db_conn()
    try:
        rows = conn.execute("SELECT text FROM responses ORDER BY id ASC").fetchall()
        return " ".join(r["text"] for r in rows)
    finally:
        conn.close()


def get_count() -> int:
    conn = db_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM responses").fetchone()
        return int(row["c"])
    finally:
        conn.close()


# =======================
# Utils
# =======================
def normalize_text(s: str) -> str:
    s = str(s or "").strip()
    # 문장 의미를 해치지 않게, 위험한 특수문자만 정리(따옴표/슬래시 등)
    s = re.sub(r"[<>\\{}[\]^`|]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def publish_update() -> None:
    with SUB_LOCK:
        dead = []
        for q in SUBSCRIBERS:
            try:
                q.put_nowait({"type": "update", "ts": time.time()})
            except Exception:
                dead.append(q)
        for q in dead:
            if q in SUBSCRIBERS:
                SUBSCRIBERS.remove(q)


def require_teacher(token: str) -> None:
    if token != TEACHER_TOKEN:
        abort(403)


def student_url() -> str:
    # 배포에서는 BASE_URL을 반드시 넣는 걸 권장
    if BASE_URL:
        return f"{BASE_URL}/s"
    # 로컬 테스트용
    return f"http://localhost:{APP_PORT}/s"


def make_qr_png(data: str) -> io.BytesIO:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

'''
def tokenize_for_korean_wc(text: str) -> str:
    tokens = []
    for tok in text.split():
        tok = tok.strip()
        if len(tok) <= 1:
            continue
        if tok in KOREAN_STOPWORDS:
            continue
        tokens.append(tok)
    if not tokens:
        tokens = ["파이썬", "코딩", "생각", "느낌"]
    return " ".join(tokens)
'''

def build_wordcloud_png() -> io.BytesIO:
    # "응답 한 줄 = 하나의 구절"로 워드클라우드 생성
    conn = db_conn()
    try:
        rows = conn.execute("SELECT text FROM responses ORDER BY id ASC").fetchall()
        phrases = [r["text"].strip() for r in rows if r["text"] and r["text"].strip()]
    finally:
        conn.close()

    if not phrases:
        phrases = ["파이썬 코딩", "생각", "느낌"]

    freq = Counter(phrases)

    font_path = FONT_PATH if FONT_PATH else None
    wc = WordCloud(
        width=1600,
        height=900,
        background_color="white",
        font_path=font_path,
        prefer_horizontal=0.9,
        max_words=120,
        collocations=False,
    ).generate_from_frequencies(freq)

    # ✅ matplotlib 없이 PIL로 바로 PNG 생성
    img = wc.to_image()  # PIL.Image
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# =======================
# HTML: 학생/교사 분리(A형)
# =======================
STUDENT_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>실시간 설문(학생)</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, "Noto Sans KR", sans-serif; margin: 18px; }
    .card { max-width: 720px; margin: 0 auto; border: 1px solid #ddd; border-radius: 14px; padding: 16px; }
    h2 { margin: 6px 0 10px; }
    input, button { font-size: 16px; padding: 12px; }
    input { width: 100%; box-sizing: border-box; }
    button { width: 100%; margin-top: 10px; cursor: pointer; }
    .hint { color: #666; font-size: 13px; line-height: 1.4; margin-top: 10px; }
    .ok { display:none; margin-top: 10px; padding: 10px; border-radius: 10px; background: #f5f7ff; }
  </style>
</head>
<body>
  <div class="card">
    <h2>파이썬 코딩에 대한 생각/느낌</h2>
    <div class="hint">한 줄로 자유롭게 적어주세요. (예: “재밌는데 에러가 무서워요”, “문법은 어렵지만 만들면 뿌듯해요”)</div>
    <input id="text" placeholder="여기에 입력" maxlength="120" autocomplete="off"/>
    <button id="submit">제출</button>
    <div class="ok" id="ok">제출 완료! (다시 제출해도 됩니다)</div>
    <div class="hint">※ 제출 내용은 교실 화면에 실시간으로 반영됩니다.</div>
  </div>

<script>
  const $ = (id)=>document.getElementById(id);

  async function submit(){
    const text = $("text").value.trim();
    if(!text) return;
    const res = await fetch("/api/submit", {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({text})
    });
    if(res.ok){
      $("text").value = "";
      $("ok").style.display = "block";
      setTimeout(()=>{$("ok").style.display="none";}, 1500);
    } else {
      alert("제출 실패! 다시 시도해 주세요.");
    }
  }

  $("submit").addEventListener("click", submit);
  $("text").addEventListener("keydown", (e)=>{ if(e.key==="Enter") submit(); });
</script>
</body>
</html>
"""

TEACHER_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>실시간 설문(교사)</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, "Noto Sans KR", sans-serif; margin: 16px; }
    .top { display:flex; gap: 14px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
    .pill { display:inline-block; padding:6px 10px; border:1px solid #ddd; border-radius:999px; }
    .wrap { display:grid; grid-template-columns: 1fr 340px; gap: 14px; align-items: start; }
    .card { border:1px solid #ddd; border-radius: 14px; padding: 14px; }
    img { width: 100%; border-radius: 14px; border:1px solid #eee; }
    button { font-size: 14px; padding: 10px 12px; border-radius: 10px; cursor:pointer; border:1px solid #ddd; background:#fff;}
    .qr { width: 100%; max-width: 280px; }
    .hint { color:#666; font-size: 12px; line-height: 1.4; }
    .big { font-size: 18px; font-weight: 700; }
  </style>
</head>
<body>
  <div class="top">
    <div>
      <div class="big">교사용 실시간 워드클라우드</div>
      <div class="hint">학생 제출이 들어오면 자동 갱신됩니다.</div>
    </div>
    <div>
      응답 수 <span class="pill" id="count">0</span>
      <button id="clear">전체 초기화</button>
    </div>
  </div>

  <div class="wrap" style="margin-top:12px;">
    <div class="card">
      <img id="wc" src="/api/wordcloud.png?ts=0&token={{token}}" alt="wordcloud"/>
      <div class="hint" style="margin-top:8px;">업데이트가 안 될 때: 페이지 새로고침(F5)</div>
    </div>

    <div class="card">
      <div class="big">학생 접속 QR</div>
      <div class="hint">학생들은 LTE/5G로 접속 가능(공개 URL 배포 필요)</div>
      <img class="qr" src="/api/qr.png?token={{token}}" alt="qr"/>
      <div class="hint" style="margin-top:8px;">
        학생용 주소: <span class="pill" id="surl"></span>
      </div>
    </div>
  </div>

<script>
  const token = "{{token}}";
  const $ = (id)=>document.getElementById(id);

  async function refreshCount(){
    const res = await fetch("/api/count");
    const data = await res.json();
    $("count").innerText = data.count;
    $("surl").innerText = data.student_url;
  }

  function refreshWordcloud(){
    $("wc").src = "/api/wordcloud.png?ts=" + Date.now() + "&token=" + encodeURIComponent(token);
  }

  async function clearAll(){
    if(!confirm("전체 응답을 초기화할까요?")) return;
    const res = await fetch("/api/clear?token=" + encodeURIComponent(token), {method:"POST"});
    if(res.ok){
      refreshCount();
      refreshWordcloud();
    } else {
      alert("초기화 실패(토큰 확인)");
    }
  }

  $("clear").addEventListener("click", clearAll);

  // SSE 수신
  const es = new EventSource("/api/stream?token=" + encodeURIComponent(token));
  es.addEventListener("message", (e)=>{
    const msg = JSON.parse(e.data);
    if(msg.type === "update"){
      refreshCount();
      refreshWordcloud();
    }
  });

  refreshCount();
</script>
</body>
</html>
"""


# =======================
# Routes
# =======================
@app.get("/")
def root():
    return redirect("/s")


@app.get("/s")
def student_page():
    return render_template_string(STUDENT_HTML)


@app.get("/t/<token>")
def teacher_page(token: str):
    require_teacher(token)
    return render_template_string(TEACHER_HTML, token=token)


@app.post("/api/submit")
def api_submit():
    data = request.get_json(silent=True) or {}
    text = normalize_text(data.get("text", ""))
    if not text:
        return jsonify({"ok": False, "error": "empty"}), 400

    add_response(text)
    publish_update()
    return jsonify({"ok": True})


@app.get("/api/count")
def api_count():
    return jsonify({"count": get_count(), "student_url": student_url()})


@app.post("/api/clear")
def api_clear():
    token = request.args.get("token", "")
    require_teacher(token)
    clear_responses()
    publish_update()
    return jsonify({"ok": True})


@app.get("/api/wordcloud.png")
def api_wordcloud():
    token = request.args.get("token", "")
    require_teacher(token)
    buf = build_wordcloud_png()
    return send_file(buf, mimetype="image/png", download_name="wordcloud.png")


@app.get("/api/qr.png")
def api_qr():
    token = request.args.get("token", "")
    require_teacher(token)
    buf = make_qr_png(student_url())
    return send_file(buf, mimetype="image/png", download_name="qr.png")


@app.get("/api/stream")
def api_stream():
    token = request.args.get("token", "")
    require_teacher(token)

    def event_stream():
        q: Queue = Queue()
        with SUB_LOCK:
            SUBSCRIBERS.append(q)

        # 연결 직후 1회
        yield f"data: {json.dumps({'type':'update','ts':time.time()})}\n\n"

        try:
            while True:
                msg = q.get()
                yield f"data: {json.dumps(msg)}\n\n"
        except GeneratorExit:
            pass
        finally:
            with SUB_LOCK:
                if q in SUBSCRIBERS:
                    SUBSCRIBERS.remove(q)

    return Response(event_stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)



