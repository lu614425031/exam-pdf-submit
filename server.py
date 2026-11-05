import argparse
import hashlib
import io
import json
import re
import secrets
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
MAX_FILE = 25 * 1024 * 1024
ADMIN = secrets.token_urlsafe(32)
PAPER_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
SCHEMA = """
CREATE TABLE IF NOT EXISTS seats (
 seat TEXT PRIMARY KEY, student TEXT UNIQUE NOT NULL,
 code TEXT UNIQUE, token TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS submissions (
 seat TEXT PRIMARY KEY REFERENCES seats(seat),
 id TEXT UNIQUE NOT NULL, filename TEXT NOT NULL, received TEXT NOT NULL,
 sha TEXT NOT NULL, original BLOB NOT NULL, receipt BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS resubmissions (
 seat TEXT PRIMARY KEY REFERENCES submissions(seat)
);
CREATE TABLE IF NOT EXISTS paper_tokens (
 digest TEXT PRIMARY KEY, claimed_by TEXT REFERENCES seats(student)
);
CREATE TABLE IF NOT EXISTS printable_tokens (
 digest TEXT PRIMARY KEY REFERENCES paper_tokens(digest), printed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, accepting INTEGER NOT NULL);
INSERT OR IGNORE INTO settings VALUES (1, 0);
"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_paper_code():
    return "".join(secrets.choice(PAPER_ALPHABET) for _ in range(8))


def receipt(student, record, received, size, sha):
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(595, 842))
    page.setTitle("Exam submission receipt " + record)
    page.saveState()
    page.translate(298, 415)
    page.rotate(32)
    page.setFillColorRGB(.88, .92, .97)
    page.setFont("Helvetica-Bold", 53)
    page.drawCentredString(0, 0, "RECEIVED")
    page.setFont("Helvetica-Bold", 16)
    page.drawCentredString(0, -28, f"STUDENT ID ENDING {student[-4:]}")
    page.restoreState()
    page.setFillColorRGB(.10, .19, .31)
    page.setFont("Helvetica-Bold", 26)
    page.drawString(55, 748, "Exam submission receipt")
    page.setFont("Helvetica", 12)
    for index, line in enumerate([
        f"Receipt: {record}", f"Student ID ending: {student[-4:]}",
        f"Received (UTC): {received}", f"File size: {size:,} bytes", "Original file SHA-256",
    ]):
        page.drawString(55, 694 - index * 32, line)
    page.setFont("Courier", 10)
    page.drawString(55, 546, sha)
    page.setFont("Helvetica", 10)
    page.drawString(55, 70, "The original submission is stored separately for grading.")
    page.showPage()
    page.save()
    return buffer.getvalue()


class RequestError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class Handler(BaseHTTPRequestHandler):
    timeout = 45

    def log_message(self, *_args):
        pass

    def answer(self, status, data=b"", mime="application/json; charset=utf-8", headers=()):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def cookie(self, name):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        return cookies[name].value if name in cookies else ""

    def body(self, limit):
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            raise RequestError("缺少有效的檔案大小。")
        size = int(length)
        if not 0 < size <= limit:
            raise RequestError("檔案為空或超過大小限制。", 413)
        result = self.rfile.read(size)
        if len(result) != size:
            raise RequestError("傳輸中斷，請重新整理後確認收件狀態。")
        return result

    def fields(self):
        try:
            data = json.loads(self.body(65536))
            if isinstance(data, dict):
                return data
        except (UnicodeError, ValueError):
            pass
        raise RequestError("資料格式錯誤。")

    def dispatch(self, post):
        path = urlsplit(self.path).path
        local = self.client_address[0] == "127.0.0.1" and self.headers.get("Host") == self.server.local_host
        if post and self.headers.get("Origin") not in (None, "http://" + self.headers.get("Host", "")):
            raise RequestError("無法從其他網站送出。", 403)
        if not post and local and path == "/teacher/" + ADMIN:
            return self.answer(303, headers=[("Location", "/teacher"),
                ("Set-Cookie", f"teacher={ADMIN}; Path=/; HttpOnly; SameSite=Strict")])
        teacher = path.startswith("/teacher")
        if teacher and not (local and secrets.compare_digest(self.cookie("teacher").encode(), ADMIN.encode())):
            raise RequestError("請在教師電腦開啟啟動視窗顯示的教師網址。", 403)

        if not post and path in ("/", "/teacher", "/app.js", "/style.css"):
            name, mime = {
                "/": ("student.html", "text/html"), "/teacher": ("teacher.html", "text/html"),
                "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
            }[path]
            return self.answer(200, (ROOT / name).read_bytes(), mime + "; charset=utf-8")

        with closing(sqlite3.connect(self.server.db, timeout=20)) as db:
            db.row_factory = sqlite3.Row
            if teacher:
                return self.teacher(db, path, post)
            if post and path == "/pair":
                fields = self.fields()
                code = str(fields.get("code", "")).strip().upper().replace("-", "")
                student = str(fields.get("student", "")).strip().upper()
                token = secrets.token_urlsafe(32)
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    eligible = db.execute("SELECT 1 FROM seats WHERE student=? AND token IS NULL", (student,)).fetchone()
                    available = db.execute("SELECT 1 FROM paper_tokens WHERE digest=? AND claimed_by IS NULL", (digest(code),)).fetchone()
                    if not eligible or not available:
                        raise RequestError("學號未登錄或已登入，或 Token 無效或已使用。請通知監考。", 403)
                    db.execute("UPDATE paper_tokens SET claimed_by=? WHERE digest=?", (student, digest(code)))
                    db.execute("UPDATE seats SET token=? WHERE student=?", (digest(token), student))
                return self.answer(200, {"ok": True}, headers=[
                    ("Set-Cookie", f"seat={token}; Path=/; HttpOnly; SameSite=Strict")])
            token = digest(self.cookie("seat"))
            seat = db.execute("SELECT seat, student FROM seats WHERE token=?", (token,)).fetchone()
            if not post and path == "/status":
                accepting = db.execute("SELECT accepting FROM settings").fetchone()[0]
                if not seat:
                    return self.answer(200, {"paired": False})
                row = db.execute("SELECT id, filename, received FROM submissions WHERE seat=?", (seat["seat"],)).fetchone()
                allowed = db.execute("SELECT 1 FROM resubmissions WHERE seat=?", (seat["seat"],)).fetchone()
                return self.answer(200, {"resubmit": bool(allowed), "paired": True, "last": seat["student"][-4:],
                                         "accepting": bool(accepting), "submission": dict(row) if row else None})
            if not seat:
                raise RequestError("請先輸入學號與紙本 Token 登入。", 403)
            if not post and path == "/receipt":
                row = db.execute("SELECT receipt FROM submissions WHERE seat=?", (seat["seat"],)).fetchone()
                if row:
                    return self.answer(200, row[0], "application/pdf",
                                       [("Content-Disposition", "attachment; filename=receipt.pdf")])
            if post and path == "/submit":
                return self.submit(db, seat, token)
            raise RequestError("找不到頁面或檔案。", 404)

    def submit(self, db, seat, token):
        if not db.execute("SELECT accepting FROM settings").fetchone()[0]:
            raise RequestError("目前未開放收件。", 409)
        name = unquote(self.headers.get("X-File-Name", ""), errors="strict").strip()
        if (not 1 <= len(name) <= 150 or name in (".", "..") or name.endswith(".")
                or any(ord(c) < 32 or c in '\\/:*?"<>|' for c in name)):
            raise RequestError("檔名無效，請重新命名後再交卷。")
        data = self.body(MAX_FILE)
        if not name.lower().endswith(".pdf") or not data.startswith(b"%PDF-"):
            raise RequestError("只接受 PDF 答案卷。")
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted or not reader.pages:
                raise RequestError("請上傳至少一頁且未加密的 PDF。")
        except (PdfReadError, ValueError, RecursionError, OSError) as error:
            raise RequestError("PDF 無法讀取，請重新匯出答案卷。") from error
        record = secrets.token_hex(8).upper()
        received = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sha = hashlib.sha256(data).hexdigest()
        pdf = receipt(seat["student"], record, received, len(data), sha)
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT accepting FROM settings").fetchone()[0]:
                raise RequestError("收件已截止。", 409)
            if not db.execute("SELECT 1 FROM seats WHERE seat=? AND token=?", (seat["seat"], token)).fetchone():
                raise RequestError("登入已更新，請重新整理後登入。", 403)
            if db.execute("SELECT 1 FROM submissions WHERE seat=?", (seat["seat"],)).fetchone():
                if not db.execute("DELETE FROM resubmissions WHERE seat=?", (seat["seat"],)).rowcount:
                    raise RequestError("此學號已交卷。", 409)
            db.execute("""INSERT INTO submissions VALUES (?,?,?,?,?,?,?)
                          ON CONFLICT(seat) DO UPDATE SET id=excluded.id, filename=excluded.filename,
                          received=excluded.received, sha=excluded.sha, original=excluded.original,
                          receipt=excluded.receipt""",
                       (seat["seat"], record, name, received, sha, data, pdf))
        return self.answer(201, {"id": record})

    def teacher(self, db, path, post):
        if not post and path == "/teacher/status":
            rows = db.execute("""SELECT student, token IS NOT NULL AS paired, id, filename, received,
                                        resubmissions.seat IS NOT NULL AS resubmit
                                 FROM seats LEFT JOIN submissions USING (seat)
                                 LEFT JOIN resubmissions USING (seat) ORDER BY student""")
            return self.answer(200, {"seats": [dict(row) for row in rows],
                                     "accepting": bool(db.execute("SELECT accepting FROM settings").fetchone()[0])})
        if post and path == "/teacher/seat":
            roster = self.fields().get("roster", "")
            if not isinstance(roster, str):
                raise RequestError("名單格式錯誤。")
            students = roster.upper().split()
            if not 1 <= len(students) <= 500 or any(
                not re.fullmatch(r"[A-Z0-9]{4,24}", student) for student in students
            ):
                raise RequestError("請輸入 1 至 500 個學號，每行一筆。")
            if len(set(students)) != len(students):
                raise RequestError("名單內有重複學號。")
            with db:
                db.executemany("INSERT INTO seats(seat,student) VALUES (?,?) ON CONFLICT(student) DO NOTHING",
                               [(student, student) for student in students])
            return self.answer(200, {"count": len(students)})
        if not post and path in ("/teacher/tokens", "/teacher/tokens/download"):
            codes = [row[0] for row in db.execute("""SELECT printed FROM printable_tokens
                     JOIN paper_tokens USING (digest) WHERE claimed_by IS NULL ORDER BY printed""")]
            if path.endswith("/download"):
                data = ("Token\r\n" + "".join(code + "\r\n" for code in codes)).encode("utf-8-sig")
                return self.answer(200, data, "text/tab-separated-values; charset=utf-8",
                                   [("Content-Disposition", "attachment; filename=paper-tokens.tsv")])
            return self.answer(200, {"codes": codes})
        if post and path == "/teacher/tokens":
            count = self.fields().get("count")
            if type(count) is not int or not 1 <= count <= 500:
                raise RequestError("請輸入 1 至 500 組。")
            codes = [new_paper_code() for _ in range(count)]
            printed = [code[:4] + "-" + code[4:] for code in codes]
            with db:
                db.executemany("INSERT INTO paper_tokens(digest) VALUES (?)", [(digest(code),) for code in codes])
                db.executemany("INSERT INTO printable_tokens VALUES (?,?)",
                               [(digest(code), label) for code, label in zip(codes, printed)])
            return self.answer(200, {"codes": printed})
        if post and path == "/teacher/resubmit":
            student = str(self.fields().get("student", "")).strip().upper()
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT seat FROM submissions JOIN seats USING (seat) WHERE student=?",
                                 (student,)).fetchone()
                if not row:
                    raise RequestError("此學號尚未交卷，無須開放重交。", 404)
                db.execute("INSERT OR IGNORE INTO resubmissions VALUES (?)", (row["seat"],))
            return self.answer(200, {"ok": True})
        if post and path == "/teacher/reset":
            student = str(self.fields().get("student", "")).strip().upper()
            with db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("UPDATE seats SET token=NULL, code=NULL WHERE student=?", (student,)).rowcount:
                    raise RequestError("查無該學號。", 404)
            return self.answer(200, {"ok": True})
        if post and path == "/teacher/accepting":
            value = self.fields().get("accepting")
            if not isinstance(value, bool):
                raise RequestError("收件狀態無效。")
            with db:
                db.execute("UPDATE settings SET accepting=?", (value,))
            return self.answer(200, {"ok": True})
        if not post and path == "/teacher/export":
            return self.export(db)
        raise RequestError("找不到頁面。", 404)

    def export(self, db):
        with tempfile.TemporaryFile() as archive:
            with ZipFile(archive, "w") as output:
                rows = db.execute("""SELECT student, submissions.* FROM submissions
                                     JOIN seats USING (seat) ORDER BY seat""")
                for row in rows:
                    folder = row["student"]
                    output.writestr(f'{folder}/original_{row["filename"]}', row["original"])
                    output.writestr(f"{folder}/receipt.pdf", row["receipt"])
            size = archive.tell()
            archive.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=exam-submissions.zip")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            shutil.copyfileobj(archive, self.wfile)

    def handle_request(self, post):
        try:
            self.dispatch(post)
        except RequestError as error:
            self.answer(error.status, {"error": error.message})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except (sqlite3.Error, OSError, UnicodeError) as error:
            print(type(error).__name__, str(error), flush=True)
            self.answer(500, {"error": "收件服務發生錯誤，請通知監考並保留原始檔。"})

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=ROOT / "one-time-exam.sqlite3")
    args = parser.parse_args()
    with ThreadingHTTPServer((args.bind, args.port), Handler) as server:
        server.db = args.data.resolve()
        server.local_host = f"127.0.0.1:{server.server_port}"
        with closing(sqlite3.connect(server.db)) as db, db:
            db.executescript(SCHEMA)
            db.execute("UPDATE settings SET accepting=0")
        print(f"Teacher: http://{server.local_host}/teacher/{ADMIN}", flush=True)
        print(f"Students: http://<teacher-ip>:{server.server_port}/", flush=True)
        print(f"Database: {server.db}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
