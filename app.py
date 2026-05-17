"""ProjectZen — Flask + SocketIO web front-end for the SF Agent."""
from gevent import monkey
monkey.patch_all()  # must be first — patches stdlib for async I/O

import os
import sys
import json
import threading
from pathlib import Path

from flask import Flask, render_template, send_file, request
from flask_socketio import SocketIO
from langchain_core.callbacks import BaseCallbackHandler
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv()

import sf_agent

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "projectzen-sf-2026")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# sid -> {"csv": path, "xlsx": path}
_files: dict = {}


class _WebCallback(BaseCallbackHandler):
    """Streams agent tool calls to the browser via SocketIO."""

    def __init__(self, sid: str):
        self.sid = sid

    def _emit(self, event: str, data: dict):
        socketio.emit(event, data, room=self.sid)

    def on_tool_start(self, serialized, input_str, **kw):
        self._emit("trace", {
            "type": "tool_start",
            "tool": serialized.get("name", "tool"),
            "input": str(input_str)[:500],
        })

    def on_tool_end(self, output, **kw):
        self._emit("trace", {
            "type": "tool_end",
            "output": str(output)[:800],
        })

    def on_llm_start(self, serialized, prompts, **kw):
        self._emit("trace", {"type": "thinking"})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/download/<fmt>")
def download(fmt: str):
    sid = request.args.get("sid", "")
    paths = _files.get(sid, {})
    path = paths.get(fmt)
    if not path or not os.path.exists(path):
        return "File not found", 404
    return send_file(path, as_attachment=True)


@socketio.on("run_query")
def on_run_query(data):
    sid = request.sid
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        socketio.emit("error", {"message": "Prompt cannot be empty."}, room=sid)
        return

    def _run():
        try:
            sf_agent._last["rows"] = []
            sf_agent._last["entity"] = None
            sf_agent._last["params"] = None

            cb = _WebCallback(sid)
            agent = sf_agent.build_agent()

            result = agent.invoke(
                {"messages": [("user", prompt)]},
                config={"callbacks": [cb]},
            )

            final = result["messages"][-1].content
            if isinstance(final, list):
                final = "".join(
                    b.get("text", "") for b in final if isinstance(b, dict)
                )

            df = sf_agent.to_dataframe()

            if df.empty:
                socketio.emit("result", {
                    "message": final,
                    "rows": [], "columns": [], "count": 0,
                    "has_csv": False, "has_xlsx": False,
                }, room=sid)
                return

            csv_path, xlsx_path = sf_agent.save_outputs(df, prompt)
            _files[sid] = {
                "csv": str(csv_path),
                "xlsx": str(xlsx_path) if xlsx_path else None,
            }

            rows = json.loads(df.fillna("").to_json(orient="records"))

            socketio.emit("result", {
                "message": final,
                "rows": rows[:500],
                "columns": list(df.columns),
                "count": len(df),
                "has_csv": True,
                "has_xlsx": xlsx_path is not None,
            }, room=sid)

        except Exception as exc:
            socketio.emit("error", {
                "message": f"{type(exc).__name__}: {exc}"
            }, room=sid)

    threading.Thread(target=_run, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print(f"ProjectZen SF Agent  →  http://localhost:{port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=debug)
