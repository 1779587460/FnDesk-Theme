# -*- coding: utf-8 -*-
"""
牛牛主题 wallpaper WebUI —— 单文件、零第三方依赖（仅 Python 标准库）

功能：
  - 浏览器页面拖拽/选择 enter 视频、idle 视频、背景图
  - 自动按文件名归类（enter/idle/图片）
  - 输入目标文件夹名，点击生成
  - 后台线程跑 ffmpeg（Alpha增强 + 左右30px羽化 + 前景右移300px + h264_nvenc 硬编）
  - 页面轮询进度，完成后预览 + 下载

启动：  python wallpaper_webui.py [port]
默认端口： 8765
浏览器自动打开 http://127.0.0.1:8765/
"""
import os
import sys
import json
import time
import uuid
import shutil
import tempfile
import threading
import subprocess
import webbrowser
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email import policy
from email.parser import BytesParser

# ---- 配置 ----
ROOT = os.path.dirname(os.path.abspath(__file__))   # D:\牛牛主题
FFMPEG = "ffmpeg"
FPS = 30
DEFAULT_PORT = 8765
MAX_UPLOAD = 512 * 1024 * 1024   # 512MB 上限，足够 3 个大视频

# ---- 任务状态 ----
JOBS = {}            # job_id -> dict
JOBS_LOCK = threading.Lock()

# 输入过滤
VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv")
BG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

def classify(files):
    """把 (name, path) 列表分类为 (enter, idle, bg)"""
    vids = [f for f in files if f[0].lower().endswith(VIDEO_EXT)]
    bgs = [f for f in files if f[0].lower().endswith(BG_EXT)]
    enter = idle = None
    for name, path in vids:
        low = name.lower()
        if "idle" in low:
            idle = (name, path)
        elif "enter" in low:
            enter = (name, path)
    leftover = [v for v in vids if v not in (enter, idle)]
    if enter is None and leftover:
        enter = leftover.pop(0)
    if idle is None and leftover:
        idle = leftover.pop(0)
    bg = bgs[0] if bgs else None
    return enter, idle, bg

def build_ffmpeg_cmd(bg, enter, idle, out):
    """生成 ffmpeg 命令（与 make_wallpaper.py 一致的默认流水线）"""
    inputs = [bg, enter] if idle is None else [bg, enter, idle]
    fc_parts = ["[0:v]scale=1920:1080[bg];"]
    fc_parts.append("[1:v]crop=1920:1080:0:0[a];")
    fc_parts.append("[1:v]crop=1920:1080:1920:0,format=gray,eq=contrast=2.0:brightness=0.05[am];")
    fc_parts.append("[a][am]alphamerge[A];")
    if idle is not None:
        fc_parts.append("[2:v]crop=1920:1080:0:0[b];")
        fc_parts.append("[2:v]crop=1920:1080:1920:0,format=gray,eq=contrast=2.0:brightness=0.05[bm];")
        fc_parts.append("[b][bm]alphamerge[B];")
        fc_parts.append("[A][B]concat=n=2:v=1:a=0[fg];")
    else:
        fc_parts.append("[A]null[fg];")
    fc_parts.append("[fg]format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*if(lt(X,30),X/30,if(gt(X,1890),(1920-X)/30,1))'[soft];")
    fc_parts.append("[bg][soft]overlay=300:0")
    filter_complex = "".join(fc_parts)

    cmd = [FFMPEG, "-y"]
    for i in inputs:
        cmd += ["-i", i]
    cmd += [
        "-filter_complex", filter_complex,
        "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "18",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-an",
        out,
    ]
    return cmd

def run_ffmpeg_job(job_id):
    """后台线程：执行 ffmpeg，更新进度"""
    job = JOBS[job_id]
    try:
        cmd = build_ffmpeg_cmd(job["bg"], job["enter"], job["idle"], job["out"])
        job["cmd"] = cmd
        job["state"] = "running"

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8", errors="replace",
        )
        # 读输出，抓进度（out_time / frame）
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            job["log"].append(line.rstrip())
            if len(job["log"]) > 400:
                job["log"] = job["log"][-400:]
            # 提取进度
            if "time=" in line:
                m = [x for x in line.split() if x.startswith("time=")]
                if m:
                    job["progress_text"] = m[0][5:]
            if "frame=" in line:
                m = [x for x in line.split() if x.startswith("frame=")]
                if m:
                    try:
                        job["frame"] = int(m[0][6:].split()[0])
                    except Exception:
                        pass

        proc.wait()
        if proc.returncode == 0 and os.path.exists(job["out"]):
            job["state"] = "done"
            job["size"] = os.path.getsize(job["out"])
        else:
            job["state"] = "error"
            job["error"] = f"ffmpeg 返回码 {proc.returncode}"
    except Exception as e:
        job["state"] = "error"
        job["error"] = str(e)

def parse_multipart(body_bytes, content_type):
    """解析 multipart/form-data（手写，兼容 Python 3.13，无 cgi 依赖）
    返回 {field_name: (filename, data_bytes)}
    """
    # 提取 boundary
    boundary = None
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            boundary = piece[len("boundary="):].strip().strip('"')
            break
    if not boundary:
        raise ValueError("Content-Type 缺少 boundary")

    delim = b"--" + boundary.encode("utf-8")
    # 按 boundary 分割
    chunks = body_bytes.split(delim)
    fields = {}
    for chunk in chunks:
        # 去掉开头 \r\n 和结尾 \r\n--\r\n
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk.startswith(b"--"):
            continue
        # 分离头部和正文（空行分隔）
        header_end = chunk.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        header_blob = chunk[:header_end].decode("utf-8", errors="replace")
        payload = chunk[header_end + 4:]
        # 解析 Content-Disposition
        name = filename = None
        for line in header_blob.split("\r\n"):
            low = line.lower()
            if low.startswith("content-disposition:"):
                for part in line.split(";"):
                    part = part.strip()
                    if part.lower().startswith("name="):
                        name = part[5:].strip().strip('"')
                    elif part.lower().startswith("filename="):
                        filename = part[9:].strip().strip('"')
        if name:
            fields[name] = (filename, payload)
    return fields

class Handler(BaseHTTPRequestHandler):
    server_version = "wallpaper-webui/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[webui] " + (fmt % args) + "\n")

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send_html(PAGE)
        elif path == "/status":
            job_id = self._query("job")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._send_json({"ok": False, "error": "job 不存在"}, 404)
                return
            resp = {
                "ok": True,
                "state": job["state"],
                "progress_text": job.get("progress_text", ""),
                "frame": job.get("frame", 0),
                "error": job.get("error"),
                "size": job.get("size"),
                "log": job["log"][-12:],
            }
            self._send_json(resp)
        elif path == "/result":
            job_id = self._query("job")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job or job["state"] != "done":
                self._send_json({"ok": False, "error": "未完成"}, 404)
                return
            out = job["out"]
            try:
                with open(out, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Disposition", "inline; filename*=UTF-8''" +
                                 __import__("urllib.parse", fromlist=["quote"]).quote(os.path.basename(out)))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def _query(self, key):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        return (qs.get(key) or [""])[0]

    # ---------- POST ----------
    def do_POST(self):
        if self.path != "/upload":
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_UPLOAD:
                self._send_json({"ok": False, "error": f"请求体大小非法: {length}"}, 400)
                return
            ctype = self.headers.get("Content-Type", "")
            body = self.rfile.read(length)
            fields = parse_multipart(body, ctype)
        except Exception as e:
            self._send_json({"ok": False, "error": f"解析上传失败: {e}"}, 400)
            return

        # 取字段
        folder = ""
        if "folder" in fields:
            fn, data = fields["folder"]
            folder = data.decode("utf-8", errors="replace").strip()

        files = []
        for key in ("enter", "idle", "bg"):
            if key in fields and fields[key][0]:
                fn, data = fields[key]
                files.append((key, fn, data))

        if len(files) < 2:
            self._send_json({"ok": False, "error": "至少需要 enter 视频 + 背景图"}, 400)
            return

        # 落盘到临时目录
        tmp_dir = tempfile.mkdtemp(prefix="wallpaper_")
        stored = []
        for key, fn, data in files:
            # key 是字段名，fn 是浏览器传来的原始文件名（用于归类）
            safe = os.path.basename(fn or f"{key}.dat")
            p = os.path.join(tmp_dir, safe)
            with open(p, "wb") as f:
                f.write(data)
            stored.append((safe, p))

        # 归类
        enter, idle, bg = classify(stored)
        if bg is None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._send_json({"ok": False, "error": "未识别到背景图（.png/.jpg/.jpeg/.webp）"}, 400)
            return
        if enter is None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._send_json({"ok": False, "error": "未识别到 enter 视频"}, 400)
            return

        # 目标文件夹
        for ch in '/\\:*?"<>|':
            folder = folder.replace(ch, "_")
        if not folder:
            folder = "牛牛新主题"
        out_dir = os.path.join(ROOT, folder)
        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, "wallpaper.mp4")

        # 创建任务
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "state": "queued",
            "bg": bg[1], "enter": enter[1], "idle": idle[1] if idle else None,
            "out": out_file,
            "folder": folder,
            "log": [],
            "progress_text": "", "frame": 0,
            "tmp_dir": tmp_dir,
            "created": time.time(),
        }
        with JOBS_LOCK:
            JOBS[job_id] = job

        t = threading.Thread(target=run_ffmpeg_job, args=(job_id,), daemon=True)
        t.start()

        self._send_json({"ok": True, "job": job_id, "folder": folder}, 200)

# ---------- HTML 页面 ----------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>牛牛主题 wallpaper 生成器</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
    background:#14151f; color:#e0e0e0; min-height:100vh; padding:32px 16px;
  }
  .wrap { max-width:760px; margin:0 auto; }
  h1 { font-size:1.5rem; margin-bottom:6px; letter-spacing:.5px; }
  .sub { color:#888; font-size:.9rem; margin-bottom:28px; }
  .card {
    background:#1e2030; border:1px solid #2c2e42; border-radius:12px;
    padding:22px; margin-bottom:16px;
  }
  .card h2 { font-size:.95rem; text-transform:uppercase; letter-spacing:1px;
             color:#8b8fa8; margin-bottom:14px; font-weight:600; }
  #drop {
    border:2px dashed #3a3d5c; border-radius:10px; padding:36px 20px;
    text-align:center; color:#8b8fa8; cursor:pointer; transition:.15s;
    background:#171927;
  }
  #drop.dragover { border-color:#5b8cff; background:#1d2237; color:#b9c4ff; }
  #drop b { color:#5b8cff; }
  .file-list { margin-top:12px; }
  .file-chip {
    display:inline-flex; align-items:center; gap:8px;
    background:#252740; border:1px solid #33365a; border-radius:20px;
    padding:5px 14px; margin:3px 6px 3px 0; font-size:.85rem;
  }
  .file-chip .tag {
    background:#3d4168; color:#aab2ff; border-radius:10px; padding:1px 8px;
    font-size:.7rem; font-weight:700; text-transform:uppercase;
  }
  .file-chip .x { color:#f0707c; cursor:pointer; font-weight:700; padding:0 2px; }
  .file-chip .x:hover { color:#ff9aa4; }
  .field { margin:14px 0; }
  .field label { display:block; font-size:.8rem; color:#8b8fa8; margin-bottom:6px; }
  .field input[type=text] {
    width:100%; background:#171927; border:1px solid #33365a; border-radius:8px;
    color:#e0e0e0; padding:10px 12px; font-size:1rem; outline:none;
  }
  .field input[type=text]:focus { border-color:#5b8cff; }
  .btn {
    width:100%; padding:13px; border:none; border-radius:8px;
    background:#5b8cff; color:#fff; font-size:1.05rem; font-weight:600;
    cursor:pointer; transition:.15s;
  }
  .btn:hover { background:#6f9bff; }
  .btn:disabled { background:#3a3d5c; cursor:not-allowed; }
  .btn.danger { background:#e05260; }
  .btn.danger:hover { background:#ef6572; }
  .status { margin-top:16px; display:none; }
  .status.show { display:block; }
  .progress-track { height:8px; background:#252740; border-radius:6px; overflow:hidden; }
  .progress-bar { height:100%; width:0%; background:#5b8cff; transition:width .4s; }
  .status-text { margin-top:10px; font-size:.85rem; color:#aab2ff; }
  .status-log {
    margin-top:10px; font-size:.72rem; color:#666;
    background:#11131c; border-radius:6px; padding:10px; max-height:180px; overflow:auto;
    font-family:Consolas,Menlo,monospace; white-space:pre-wrap; word-break:break-all;
  }
  .result { margin-top:18px; }
  .result video { width:100%; border-radius:10px; background:#000; }
  .dl-row { display:flex; gap:10px; margin-top:10px; }
  .dl-row a { flex:1; text-align:center; padding:11px; border-radius:8px;
              text-decoration:none; font-weight:600; }
  .dl { background:#2ea06b; color:#fff; }
  .dl:hover { background:#36b77a; }
  .err { color:#f0707c; font-size:.9rem; margin-top:10px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>🎬 牛牛主题 wallpaper 生成器</h1>
  <div class="sub">enter + idle + 背景图 → 1920×1080 wallpaper.mp4（Alpha增强 · 羽化 · 右移300px · NVENC）</div>

  <div class="card">
    <h2>① 素材（自动识别 enter / idle / 背景）</h2>
    <div id="drop">
      把文件拖到这里，或<b>点击选择</b><br>
      <span style="font-size:.8rem;color:#666">支持 enter.mp4 + idle.mp4 + 背景图（png/jpg/webp）一次多选</span>
    </div>
    <input type="file" id="fileInput" multiple accept=".mp4,.webm,.mov,.mkv,.png,.jpg,.jpeg,.webp,.bmp" style="display:none">
    <div class="file-list" id="fileList"></div>
  </div>

  <div class="card">
    <h2>② 目标文件夹名</h2>
    <div class="field">
      <input type="text" id="folder" placeholder="例如：牛牛提弗洛斯（留空默认 牛牛新主题）">
    </div>
    <button class="btn" id="genBtn" onclick="startGen()">生成 wallpaper.mp4</button>

    <div class="status" id="status">
      <div class="progress-track"><div class="progress-bar" id="pbar"></div></div>
      <div class="status-text" id="stext">处理中…</div>
      <div class="status-log" id="slog"></div>
      <div class="err" id="serr"></div>
      <div class="result" id="sresult" style="display:none">
        <video id="preview" controls muted></video>
        <div class="dl-row">
          <a class="dl" id="dlLink" href="#">⬇ 下载 wallpaper.mp4</a>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const drop = document.getElementById("drop");
const fileInput = document.getElementById("fileInput");
const fileList = document.getElementById("fileList");
let pending = [];   // [{name, size}]

drop.addEventListener("click", () => fileInput.click());
drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("dragover"); });
drop.addEventListener("dragleave", () => drop.classList.remove("dragover"));
drop.addEventListener("drop", e => {
  e.preventDefault(); drop.classList.remove("dragover");
  addFiles(e.dataTransfer.files);
});
fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });

function addFiles(list) {
  for (const f of list) pending.push(f);
  renderFiles();
}
function removeFile(i) { pending.splice(i, 1); renderFiles(); }
function renderFiles() {
  fileList.innerHTML = "";
  pending.forEach((f, i) => {
    const tag = tagOf(f.name);
    const div = document.createElement("span");
    div.className = "file-chip";
    div.innerHTML = `<span class="tag">${tag}</span>${esc(f.name)}<span style="color:#666;font-size:.75rem">${fmtSize(f.size)}</span>
      <span class="x" onclick="removeFile(${i})">×</span>`;
    fileList.appendChild(div);
  });
}
function tagOf(name) {
  const n = name.toLowerCase();
  if (n.endsWith(".png")||n.endsWith(".jpg")||n.endsWith(".jpeg")||n.endsWith(".webp")||n.endsWith(".bmp")) return "BG";
  if (n.includes("idle")) return "IDLE";
  if (n.includes("enter")) return "ENTER";
  return "VIDEO";
}
function esc(s) { return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function fmtSize(n){ if(n>=1048576) return (n/1048576).toFixed(1)+" MB"; return Math.round(n/1024)+" KB"; }

// 归类（与后端一致），元素就是 File 对象
function classify() {
  const vids = pending.filter(f => /\.(mp4|webm|mov|mkv)$/i.test(f.name));
  const bgs = pending.filter(f => /\.(png|jpg|jpeg|webp|bmp)$/i.test(f.name));
  let enter = null, idle = null;
  vids.forEach(v => {
    const n = v.name.toLowerCase();
    if (n.includes("idle")) idle = v;
    else if (n.includes("enter")) enter = v;
  });
  const leftover = vids.filter(v => v !== enter && v !== idle);
  if (!enter && leftover.length) enter = leftover.shift();
  if (!idle && leftover.length) idle = leftover.shift();
  return { enter, idle, bg: bgs[0] };
}

async function startGen() {
  const cls = classify();
  if (!cls.enter || !cls.bg) {
    alert("需要至少 enter 视频 + 背景图");
    return;
  }
  const folder = document.getElementById("folder").value.trim();
  const fd = new FormData();
  fd.append("folder", folder || "牛牛新主题");
  fd.append("enter", cls.enter);
  if (cls.idle) fd.append("idle", cls.idle);
  if (cls.bg) fd.append("bg", cls.bg);

  setBusy(true);
  showStatus();
  try {
    const resp = await fetch("/upload", { method: "POST", body: fd });
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error);
    poll(data.job);
  } catch (e) {
    showError(e.message);
    setBusy(false);
  }
}

let pollTimer = null;
function poll(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const resp = await fetch(`/status?job=${jobId}`);
      const s = await resp.json();
      if (!s.ok) return;
      if (s.state === "running" || s.state === "queued") {
        document.getElementById("pbar").style.width = "35%";
        document.getElementById("stext").textContent =
          `处理中… ${s.frame || 0} 帧 · ${s.progress_text || ""}`;
        const log = document.getElementById("slog");
        if (s.log && s.log.length) log.textContent = s.log[s.log.length-1];
      } else if (s.state === "done") {
        clearInterval(pollTimer);
        document.getElementById("pbar").style.width = "100%";
        document.getElementById("stext").textContent =
          `完成 ✓ ${s.size ? fmtSize(s.size) : ""}`;
        showResult(jobId);
        setBusy(false);
      } else if (s.state === "error") {
        clearInterval(pollTimer);
        showError(s.error || "生成失败");
        if (s.log && s.log.length) {
          document.getElementById("slog").textContent =
            s.log.slice(-8).join("\\n");
        }
        setBusy(false);
      }
    } catch (e) { /* keep polling */ }
  }, 800);
}

function showResult(jobId) {
  const r = document.getElementById("sresult");
  r.style.display = "block";
  const vid = document.getElementById("preview");
  vid.src = `/result?job=${jobId}`;
  document.getElementById("dlLink").href = `/result?job=${jobId}`;
  vid.load();
}
function showStatus() {
  document.getElementById("status").classList.add("show");
  document.getElementById("sresult").style.display = "none";
  document.getElementById("serr").textContent = "";
  document.getElementById("slog").textContent = "";
  document.getElementById("pbar").style.width = "5%";
  document.getElementById("stext").textContent = "上传中…";
}
function showError(msg) {
  document.getElementById("serr").textContent = "✗ " + msg;
  document.getElementById("pbar").style.width = "0%";
}
function setBusy(b) {
  document.getElementById("genBtn").disabled = b;
}
</script>
</body>
</html>
"""

def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 56)
    print(" 牛牛主题 wallpaper WebUI 已启动")
    print(f" 地址: {url}")
    print(" 按 Ctrl+C 停止")
    print("=" * 56)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")

if __name__ == "__main__":
    main()
