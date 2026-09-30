"""Local-only operation panel for the JEV Model Router.

This module intentionally uses only the Python standard library for the HTTP
surface. It reuses the production Router, RealJEVClassifier, model registry,
and OpenCode Go provider rather than implementing a second routing path.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .opencode_go import OpenCodeGoProvider
from .providers import ModelRequest, ModelResponse
from .real_jev import RealJEVClassifier
from .registry import ModelDefinition, ModelRegistry
from .router import Router
from .schemas import RouteRequest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "models.real-pilot.yaml"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_BUDGET = 1.50
DEFAULT_TIMEOUT = 300.0
MAX_BODY_BYTES = 1024 * 1024

_CAPABILITY_ESTIMATES = {
    "low": 0.10,
    "medium": 0.25,
    "high": 1.00,
}


class _LazyRealJEV:
    """Instantiate the real JEV client only when automatic routing needs it."""

    def __init__(self, api_key_file: Optional[str]) -> None:
        self._api_key_file = api_key_file or None
        self._client: Optional[RealJEVClassifier] = None

    def _get(self) -> RealJEVClassifier:
        if self._client is None:
            self._client = RealJEVClassifier(api_key_file=self._api_key_file)
        return self._client

    def classify(self, request: RouteRequest):
        return self._get().classify(request)

    def resolve_cost(self, request: RouteRequest, classifier):
        return self._get().resolve_cost(request, classifier)

    @property
    def last_call_metrics(self) -> Optional[Dict[str, Any]]:
        if self._client is None:
            return None
        metrics = self._client.last_call_metrics
        return None if metrics is None else dict(metrics)


class _CaptureProvider:
    """Pass through to OpenCode Go while retaining only the latest safe response."""

    def __init__(self, provider: OpenCodeGoProvider) -> None:
        self._provider = provider
        self.last_response: Optional[ModelResponse] = None

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.last_response = None
        response = self._provider.invoke(request)
        self.last_response = response
        return response

    @property
    def last_call_metrics(self) -> Optional[Dict[str, Any]]:
        metrics = self._provider.last_call_metrics
        return None if metrics is None else dict(metrics)


def _estimate_for(definition: ModelDefinition) -> float:
    return _CAPABILITY_ESTIMATES[definition.capability.value]


class PanelRuntime:
    """Stateless per-run adapter between the web panel and the existing Router."""

    def __init__(
        self,
        registry_path: Path = DEFAULT_REGISTRY_PATH,
        *,
        provider_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.registry_path = Path(registry_path)
        self.provider_timeout = provider_timeout

    def config(self) -> Dict[str, Any]:
        registry = ModelRegistry.from_yaml(self.registry_path)
        models = []
        for name in registry.enabled_names():
            definition = registry.get(name)
            models.append(
                {
                    "name": name,
                    "model_id": definition.model_id,
                    "capability": definition.capability.value,
                }
            )
        return {
            "models": models,
            "default_budget": DEFAULT_BUDGET,
            "default_workspace": str(PROJECT_ROOT),
            "jev_api_key_file_configured": bool(os.environ.get("JEV_API_KEY_FILE")),
        }

    def execute(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("任务内容不能为空")
        prompt = prompt.strip()

        manual_model = payload.get("model")
        if manual_model in {None, "", "auto"}:
            manual_model = None
        elif not isinstance(manual_model, str):
            raise ValueError("模型选择无效")

        workspace_raw = payload.get("workspace") or str(PROJECT_ROOT)
        if not isinstance(workspace_raw, str):
            raise ValueError("工作目录无效")
        workspace = Path(workspace_raw).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("工作目录不存在")

        budget_raw = payload.get("budget", DEFAULT_BUDGET)
        try:
            budget = float(budget_raw)
        except (TypeError, ValueError):
            raise ValueError("预算必须是数字") from None
        if budget < 0:
            raise ValueError("预算不能小于 0")

        allow_auto = bool(payload.get("allow_auto", False))
        jev_api_key_file = payload.get("jev_api_key_file")
        if jev_api_key_file in {None, ""}:
            jev_api_key_file = None
        elif not isinstance(jev_api_key_file, str):
            raise ValueError("JEV API Key 文件路径无效")
        elif not Path(jev_api_key_file).expanduser().is_file():
            raise ValueError("JEV API Key 文件不存在，请检查面板中填写的文件路径")

        if (
            manual_model is None
            and jev_api_key_file is None
            and not os.environ.get("JEV_API_KEY_FILE")
        ):
            raise ValueError(
                "自动路由需要 JEV API Key。请在“JEV API Key 文件路径”中填写本机 Key 文件路径，"
                "或设置环境变量 JEV_API_KEY_FILE。"
            )

        registry = ModelRegistry.from_yaml(self.registry_path)
        if manual_model is not None:
            if manual_model not in registry.enabled_names():
                raise ValueError("所选模型不可用")

        providers: Dict[str, _CaptureProvider] = {}
        estimates: Dict[str, float] = {}
        for name in registry.enabled_names():
            definition = registry.get(name)
            providers[name] = _CaptureProvider(
                OpenCodeGoProvider(timeout=self.provider_timeout)
            )
            estimates[name] = _estimate_for(definition)

        jev = _LazyRealJEV(jev_api_key_file)
        safe_default = (
            "high_model"
            if "high_model" in registry.enabled_names()
            else registry.enabled_names()[-1]
        )
        router = Router(
            registry,
            providers,
            jev,
            safe_default_model=safe_default,
            estimated_max_costs=estimates,
            classifier_cost_resolver=jev.resolve_cost,
        )

        request = RouteRequest(
            task_id=f"panel-{uuid.uuid4().hex[:12]}",
            prompt=prompt,
            manual_model=manual_model,
            budget_limit=budget,
            metadata={
                "cwd": str(workspace),
                "auto": allow_auto,
                "source": "local_panel",
            },
        )
        result = router.route(request)

        response: Optional[ModelResponse] = None
        selected_provider: Optional[_CaptureProvider] = None
        if result.selected_model is not None:
            selected_provider = providers.get(result.selected_model)
            if selected_provider is not None:
                response = selected_provider.last_response

        definition = (
            registry.get(result.selected_model)
            if result.selected_model is not None
            else None
        )

        return {
            "ok": result.status == "success",
            "answer": "" if response is None else response.text,
            "route": result.model_dump(mode="json"),
            "selected_model_id": None if definition is None else definition.model_id,
            "provider_metrics": (
                None
                if selected_provider is None
                else selected_provider.last_call_metrics
            ),
            "jev_metrics": jev.last_call_metrics,
        }


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _error_payload(exc: Exception) -> Dict[str, Any]:
    message = str(exc).strip() or type(exc).__name__
    if len(message) > 500:
        message = message[:500] + "…"
    return {
        "ok": False,
        "error": {
            "type": type(exc).__name__,
            "message": message,
        },
    }


def make_handler(runtime: PanelRuntime):
    class Handler(BaseHTTPRequestHandler):
        server_version = "JEVModelRouterPanel/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(
            self, payload: Mapping[str, Any], status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                body = PANEL_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/config":
                try:
                    self._send_json({"ok": True, **runtime.config()})
                except Exception as exc:
                    self._send_json(_error_payload(exc), HTTPStatus.BAD_REQUEST)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path != "/api/run":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "0")
            except ValueError:
                self._send_json(
                    {"ok": False, "error": {"message": "请求长度无效"}},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(
                    {"ok": False, "error": {"message": "请求内容大小无效"}},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是 JSON 对象")
                result = runtime.execute(payload)
            except Exception as exc:
                self._send_json(_error_payload(exc), HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result)

    return Handler


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    open_browser: bool = False,
    runtime: Optional[PanelRuntime] = None,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("操作面板只允许监听本机地址")
    runtime = runtime or PanelRuntime()
    server = ThreadingHTTPServer((host, port), make_handler(runtime))
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"JEV Model Router 操作面板: {url}", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="JEV Model Router 本地操作面板")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args(argv)
    serve(args.host, args.port, open_browser=args.open_browser)
    return 0


PANEL_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JEV Model Router</title>
<style>
:root{color-scheme:dark;--bg:#0b0d10;--card:#13171c;--muted:#8b949e;--line:#2a3038;--text:#eef2f6;--accent:#6ea8fe;--ok:#52c77a;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:28px 20px 56px}.top{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-bottom:20px}
h1{margin:0;font-size:26px}.sub{color:var(--muted);margin-top:6px}.badge{padding:6px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
.grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr);gap:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
label{display:block;color:#c8d0d9;margin:0 0 7px;font-weight:600}.hint{font-size:12px;color:var(--muted);margin-top:6px}
textarea,input,select{width:100%;background:#0e1216;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:11px 12px;font:inherit;outline:none}
textarea:focus,input:focus,select:focus{border-color:var(--accent)}textarea{min-height:230px;resize:vertical}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}.field{margin-top:14px}
.switch{display:flex;gap:10px;align-items:center;margin-top:16px;padding:12px;border:1px solid var(--line);border-radius:10px}.switch input{width:auto}
button{width:100%;margin-top:16px;padding:12px 16px;border:0;border-radius:10px;background:var(--accent);color:#07111f;font-weight:750;font-size:15px;cursor:pointer}button:disabled{opacity:.5;cursor:wait}
.status{display:flex;gap:8px;align-items:center;font-weight:700}.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
.meta{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}.kv{padding:10px;border:1px solid var(--line);border-radius:9px}.k{font-size:11px;color:var(--muted);text-transform:uppercase}.v{margin-top:4px;word-break:break-word}
.answer{white-space:pre-wrap;background:#0e1216;border:1px solid var(--line);border-radius:10px;padding:14px;min-height:180px;margin-top:14px;overflow:auto}
.details{white-space:pre-wrap;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#bdc7d3;background:#0e1216;border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:12px;max-height:300px;overflow:auto}
small{color:var(--muted)}@media(max-width:850px){.grid{grid-template-columns:1fr}.top{align-items:start;flex-direction:column}.row,.meta{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div><h1>JEV Model Router</h1><div class="sub">本地操作面板 · JEV 判断 → Router 选模型 → OpenCode Go 执行</div></div>
    <div class="badge">仅监听 127.0.0.1</div>
  </div>
  <div class="grid">
    <section class="card">
      <label for="prompt">任务</label>
      <textarea id="prompt" placeholder="例如：检查这个项目的报错，修复后告诉我改了什么。"></textarea>
      <div class="row">
        <div>
          <label for="model">模型</label>
          <select id="model"><option value="auto">自动路由（推荐）</option></select>
        </div>
        <div>
          <label for="budget">单任务预算上限（$）</label>
          <input id="budget" type="number" min="0" step="0.01" value="1.50">
        </div>
      </div>
      <div class="field">
        <label for="workspace">工作目录</label>
        <input id="workspace" type="text">
        <div class="hint">OpenCode Go 只会在你填写的工作目录中执行。</div>
      </div>
      <div class="field">
        <label for="jevkey">JEV API Key 文件路径（自动路由时需要）</label>
        <input id="jevkey" type="text" placeholder="留空则使用环境变量 JEV_API_KEY_FILE">
        <div id="jevHint" class="hint">自动路由必须配置 JEV Key；手动选择模型可跳过 JEV。</div>
      </div>
      <label class="switch"><input id="allowAuto" type="checkbox"><span><b>允许 OpenCode 自动执行修改</b><br><small>打开后会向 OpenCode Go 传入 --auto；关闭更适合只读咨询。</small></span></label>
      <button id="run">执行任务</button>
    </section>

    <section class="card">
      <div class="status"><span id="dot" class="dot"></span><span id="status">等待任务</span></div>
      <div class="meta">
        <div class="kv"><div class="k">路由来源</div><div id="routeSource" class="v">—</div></div>
        <div class="kv"><div class="k">选中模型</div><div id="selectedModel" class="v">—</div></div>
        <div class="kv"><div class="k">JEV 判断</div><div id="jev" class="v">—</div></div>
        <div class="kv"><div class="k">成本</div><div id="cost" class="v">—</div></div>
      </div>
      <div id="answer" class="answer">执行后的最终结果会显示在这里。</div>
      <details><summary style="margin-top:12px;cursor:pointer;color:#c8d0d9">查看路由详情</summary><div id="details" class="details">—</div></details>
    </section>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
let jevEnvConfigured=false;
async function init(){
  try{
    const r=await fetch('/api/config',{cache:'no-store'}); const c=await r.json();
    if(!c.ok) throw new Error(c.error?.message||'配置读取失败');
    $('workspace').value=c.default_workspace||'';
    $('budget').value=c.default_budget??1.5;
    jevEnvConfigured=!!c.jev_api_key_file_configured;
    const savedKeyPath=localStorage.getItem('jevKeyFilePath')||'';
    if(savedKeyPath) $('jevkey').value=savedKeyPath;
    for(const m of c.models||[]){
      const o=document.createElement('option'); o.value=m.name;
      o.textContent=`${m.name} · ${m.model_id} · ${m.capability}`; $('model').appendChild(o);
    }
    if(jevEnvConfigured){
      $('jevkey').placeholder='已检测到环境变量 JEV_API_KEY_FILE';
      $('jevHint').textContent='已检测到环境变量 JEV_API_KEY_FILE，可直接使用自动路由。';
    }
  }catch(e){setState(false,'面板配置失败：'+e.message)}
}
function setState(ok,text){
  $('status').textContent=text; $('dot').className='dot '+(ok===true?'ok':ok===false?'bad':'');
}
function show(data){
  const route=data.route||{}; const cls=route.classifier;
  $('routeSource').textContent=route.route_source||'—';
  $('selectedModel').textContent=[route.selected_model,data.selected_model_id].filter(Boolean).join(' / ')||'—';
  $('jev').textContent=cls?`${cls.task_type} · 难度 ${cls.difficulty_score}/10 · 置信度 ${Math.round(cls.confidence*100)}%`:'—';
  const cost=route.cost; $('cost').textContent=cost?`$${Number(cost.total_production_cost||0).toFixed(6)}`:'—';
  $('answer').textContent=data.answer || (route.errors?.length?route.errors.map(x=>x.message||x.code).join('\n'):'没有返回文本');
  $('details').textContent=JSON.stringify(data,null,2);
}
$('run').addEventListener('click',async()=>{
  const prompt=$('prompt').value.trim(); if(!prompt){setState(false,'请先输入任务');return}
  const keyPath=$('jevkey').value.trim();
  if($('model').value==='auto' && !keyPath && !jevEnvConfigured){
    const message='自动路由需要 JEV API Key。请先填写下面的“JEV API Key 文件路径”。';
    setState(false,message); $('answer').textContent=message; return;
  }
  if(keyPath) localStorage.setItem('jevKeyFilePath',keyPath);
  $('run').disabled=true; setState(null,'执行中…'); $('answer').textContent='正在调用 Router / OpenCode Go…';
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      prompt,model:$('model').value,budget:$('budget').value,workspace:$('workspace').value.trim(),
      jev_api_key_file:keyPath,allow_auto:$('allowAuto').checked
    })});
    const data=await r.json();
    if(!r.ok) throw new Error(data.error?.message||'执行失败');
    show(data); setState(data.ok,data.ok?'执行完成':'路由/执行失败');
  }catch(e){setState(false,e.message);$('answer').textContent=e.message}
  finally{$('run').disabled=false}
});
init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
