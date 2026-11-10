"""录音笔记启动器：前台运行服务，关闭此窗口即停止服务（不会在后台常驻）"""
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")
os.system("title 录音笔记 - 关闭此窗口即停止服务")

from app.config import CFG  # noqa: E402  (同时把缓存/临时目录指向工作区)

HOST, PORT = CFG["server"]["host"], CFG["server"]["port"]
URL = f"http://127.0.0.1:{PORT}"  # 本机始终用回环地址打开


def port_busy() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def open_when_ready():
    for _ in range(120):
        try:
            urllib.request.urlopen(f"{URL}/info", timeout=2)
            if "--no-browser" not in sys.argv:
                webbrowser.open(URL)
            print(f"\n已在浏览器打开 {URL}")
            print("关闭此窗口（或按 Ctrl+C）即停止服务。正在处理的任务会中断，之后可在网页里重跑。\n")
            return
        except Exception:
            time.sleep(1)
    print("服务启动超时，请查看上方报错信息。")


if __name__ == "__main__":
    if port_busy():
        print(f"端口 {PORT} 已被占用（可能已经开着一个启动器窗口），直接打开网页。")
        webbrowser.open(URL)
        time.sleep(3)
        sys.exit(0)
    print("录音笔记 正在启动…")
    from app import relay

    relay.bind_children_to_self()  # 关闭窗口时连同 SSH 隧道一起结束
    threading.Thread(target=open_when_ready, daemon=True).start()
    relay.start(PORT)  # 手机/平板经 VPS 访问（config 中 relay.enabled）
    import uvicorn

    from app.main import app

    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    except KeyboardInterrupt:
        pass
