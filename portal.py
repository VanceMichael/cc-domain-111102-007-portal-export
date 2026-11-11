import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
import uuid

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


def handler_for(state, catalogues):
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

        def do_GET(self):
            path = urlparse(self.path).path
            with state.lock:
                if path == "/login":
                    return self.send(200, '<meta charset="utf-8"><h1>本地演示门户</h1><form method="post" action="/login"><button>演示登录</button></form>')
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
                    sid = uuid.uuid4().hex
                    state.data["sessions"].append(sid)
                    state.save()
                    return self.send(303, b"", headers={"Location": "/workspace", "Set-Cookie": f"venue_session={sid}; HttpOnly; SameSite=Lax; Path=/"})
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
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), handler_for(PortalState(args.state), load_catalogues()))
    print(f"本地门户：http://{args.host}:{server.server_port}/login", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
