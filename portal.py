import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlparse
import uuid

# 支持以 `python -I portal.py` 从任意工作目录启动：显式把仓库根加入路径，
# 不依赖 cwd 隐式注入，避免下载目录中的同名模块被导入。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.captcha import CaptchaStore
from app.catalogue import export_csv, load_catalogues


class PortalState:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"sessions": [], "jobs": {}}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, ensure_ascii=False))
        temp.replace(self.path)


LOGIN_PAGE = '''<!doctype html><meta charset="utf-8"><title>本地演示门户登录</title>
<h1>本地演示门户</h1>
<p style="color:#a00">{error}</p>
<form method="post" action="/login">
  <input type="hidden" name="cid" value="{cid}">
  <p><label>演示账号 <input name="username" autocomplete="username" value="{username}"></label></p>
  <p><label>密码 <input name="password" type="password" autocomplete="current-password"></label></p>
  <p><label>图形验证码 <input name="captcha" autocomplete="off" maxlength="4"></label>
     <img src="/captcha/{cid}.bmp" alt="验证码图片" width="{cw}" height="{ch}"></p>
  <button>演示登录</button>
</form>
<p><small>账号密码仅用于本地演示；验证码由人工识别，采集流程不会尝试绕过。</small></p>'''


def handler_for(state, catalogues, captchas, credentials):
    demo_user, demo_password = credentials

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type="text/html; charset=utf-8", headers=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def session(self):
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get("venue_session")
            return value.value if value else ""

        def authorized(self):
            return self.session() in state.data["sessions"]

        def login_form(self, error="", username=""):
            cid, _ = captchas.issue()
            return LOGIN_PAGE.format(error=error, username=username, cid=cid, cw=188, ch=50)

        def do_GET(self):
            path = urlparse(self.path).path
            with state.lock:
                if path == "/login":
                    return self.send(200, self.login_form())
                if path.startswith("/captcha/"):
                    cid = path.rsplit("/", 1)[-1].removesuffix(".bmp")
                    image = captcha_images.get(cid)
                    if image is None:
                        return self.send(404, "验证码不存在或已过期")
                    return self.send(200, image, "image/bmp",
                                     {"Cache-Control": "no-store"})
                if path.startswith("/test/captcha/"):
                    cid = path.rsplit("/", 1)[-1]
                    answer = captchas.test_answer(cid)
                    if answer is None:
                        return self.send(404, "not available")
                    return self.send(200, answer, "text/plain")
                if path == "/expire":
                    state.data["sessions"] = [s for s in state.data["sessions"] if s != self.session()]
                    state.save()
                    return self.send(200, '<meta charset="utf-8">演示会话已结束<a href="/login">重新登录</a>')
                if not self.authorized():
                    if path.startswith(("/jobs", "/download")):
                        return self.send(401, json.dumps({"error": "session_expired"}), "application/json")
                    return self.send(302, b"", headers={"Location": "/login"})
                if path in {"/", "/workspace"}:
                    return self.send(200, '<meta charset="utf-8"><h1>场馆资料导出</h1><a href="/expire">结束会话</a><iframe title="目录操作区" src="/frame"></iframe>')
                if path == "/frame":
                    return self.send(200, FRAME)
                if path == "/jobs":
                    # 同一演示使用者重新登录后仍能查到先前创建的任务。
                    jobs = state.data["jobs"]
                    for job in jobs.values():
                        job["polls"] += 1
                        job["state"] = "ready" if job["polls"] >= job["ready_after"] else "pending"
                    state.save()
                    return self.send(200, json.dumps(list(jobs.values()), ensure_ascii=False), "application/json")
                if path.startswith("/download/"):
                    job = state.data["jobs"].get(path.rsplit("/", 1)[-1])
                    if not job:
                        return self.send(404, "任务不存在")
                    if job["state"] != "ready":
                        return self.send(409, "导出尚未完成")
                    rows = catalogues[job["venue"]][job["language"]]
                    return self.send(200, export_csv(job["venue"], job["language"], rows), "text/csv; charset=utf-8",
                                     {"Content-Disposition": 'attachment; filename="catalogue.csv"', "X-Export-ID": job["id"]})
                self.send(404, "页面不存在")

        def do_POST(self):
            path = urlparse(self.path).path
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with state.lock:
                if path == "/login":
                    fields = parse_qs(body.decode())
                    username = fields.get("username", [""])[0]
                    password = fields.get("password", [""])[0]
                    cid = fields.get("cid", [""])[0]
                    guess = fields.get("captcha", [""])[0]
                    ok_user = username == demo_user and password == demo_password
                    ok_captcha = captchas.verify(cid, guess)
                    if not (ok_user and ok_captcha):
                        reason = "账号或密码错误" if not ok_user else "验证码错误或已过期"
                        return self.send(401, self.login_form(reason, username))
                    sid = uuid.uuid4().hex
                    state.data["sessions"].append(sid)
                    state.save()
                    return self.send(303, b"", headers={
                        "Location": "/workspace",
                        "Set-Cookie": f"venue_session={sid}; HttpOnly; SameSite=Lax; Path=/",
                    })
                if not self.authorized():
                    return self.send(401, '{"error":"session_expired"}', "application/json")
                if path == "/export":
                    fields = parse_qs(body.decode())
                    venue = fields.get("venue", [""])[0]
                    language = fields.get("language", [""])[0]
                    if venue not in catalogues or language not in catalogues[venue]:
                        return self.send(400, '{"error":"invalid_selection"}', "application/json")
                    job_id = uuid.uuid4().hex
                    job = {"id": job_id, "venue": venue, "language": language, "polls": 0,
                           "ready_after": 4 if venue == "museum-a" else 1, "state": "pending"}
                    state.data["jobs"][job_id] = job
                    state.save()
                    return self.send(200, json.dumps(job), "application/json")
                self.send(404, "页面不存在")
    return Handler


# 登录表单需要引用刚签发的验证码图片，进程内短期缓存即可。
captcha_images: dict[str, bytes] = {}


class ImageCaptchaStore(CaptchaStore):
    """签发验证码时缓存位图，供 /captcha/<id>.bmp 读取。"""

    def issue(self):
        cid, image = super().issue()
        captcha_images[cid] = image
        # 控制缓存规模，与验证码 TTL 同量级清理。
        if len(captcha_images) > 200:
            for old_cid in list(captcha_images)[:100]:
                captcha_images.pop(old_cid, None)
        return cid, image


FRAME = '''<!doctype html><meta charset="utf-8"><h2>公开服务目录</h2>
<form id="export"><label>场馆<select name="venue"><option value="museum-a">合成甲馆</option><option value="museum-b">合成乙馆</option></select></label>
<label>语言<select name="language"><option value="zh">中文</option><option value="en">English</option></select></label>
<button>生成目录文件</button></form><button id="refresh">刷新任务</button><p id="message"></p><ul id="jobs"></ul>
<script>
async function render() {
  const response = await fetch('/jobs');
  if (response.status === 401) { document.querySelector('#message').textContent = '登录已失效，请重新登录'; return; }
  const jobs = await response.json();
  const list = document.querySelector('#jobs'); list.replaceChildren();
  for (const job of jobs) {
    const row = document.createElement('li'); row.dataset.jobId = job.id;
    row.textContent = job.venue + ' / ' + job.language + ' / ' + job.state + ' / ' + job.id;
    if (job.state === 'ready') { const link = document.createElement('a'); link.href = '/download/' + job.id; link.textContent = '下载'; row.append(link); }
    list.append(row);
  }
}
document.querySelector('#refresh').onclick = render;
document.querySelector('#export').onsubmit = async event => {
  event.preventDefault();
  const response = await fetch('/export', {method:'POST',body:new URLSearchParams(new FormData(event.target))});
  if (response.status === 401) { document.querySelector('#message').textContent = '登录已失效，请重新登录'; return; }
  if (!response.ok) { document.querySelector('#message').textContent = '导出请求未成功'; return; }
  const job = await response.json(); document.querySelector('#message').textContent = '已创建任务 ' + job.id;
  // 重建表单节点，模拟门户局部刷新。
  const select = document.querySelector('select[name=venue]'); const copy = select.cloneNode(true); copy.value = select.value; select.replaceWith(copy);
  await render();
};
render();
</script>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--state", type=Path, default=Path("runtime/portal.json"))
    parser.add_argument("--test-mode", action="store_true",
                        help="开放测试用验证码答案接口 /test/captcha/，仅供本地自动化回归")
    parser.add_argument("--user", default=os.environ.get("PORTAL_DEMO_USER", "demo"))
    parser.add_argument("--password", default=os.environ.get("PORTAL_DEMO_PASSWORD", "demo12345"))
    args = parser.parse_args()

    captchas = ImageCaptchaStore(test_mode=args.test_mode)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        handler_for(PortalState(args.state), load_catalogues(), captchas, (args.user, args.password)),
    )
    print(f"本地门户：http://{args.host}:{server.server_port}/login", flush=True)
    print(f"演示账号：{args.user} / {args.password}（仅本地演示）", flush=True)
    if args.test_mode:
        print("测试模式：/test/captcha/<id> 可读取验证码答案", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
