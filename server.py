import json
import os
import re
import time
import hashlib
import requests
from flask import Flask, request, Response, jsonify
from threading import Lock

# ===================== 配置 =====================
DEBUG_MODE = os.getenv("DEBUG", "1") == "1"
UPSTREAM_URL = os.getenv("BBD_UPSTREAM", "https://app.backboard.io/api")
API_KEY = os.getenv("BBD_API_KEY", "")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "10088"))
THREAD_TTL = int(os.getenv("THREAD_TTL", "1800"))  # 秒，默认30分钟

app = Flask(__name__)
lock = Lock()

GLOBAL_AID: str = ""
thread_cache: dict[tuple, dict] = {}

# 预热 thread 池：保持 N 个空闲 thread，新对话直接取用
THREAD_POOL_SIZE = 4
_thread_pool: list[str] = []   # 空闲 thread_id 列表
_pool_lock = Lock()

# tool_use_id → thread_id 映射：用于 tool_result 续接同一 thread
_tool_tid_map: dict[str, str] = {}
_tool_tid_lock = Lock()

HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

# ===================== 工具函数 =====================
def log(msg, level="INFO"):
    if not DEBUG_MODE and level == "DEBUG":
        return
    color = {"INFO": "\033[94m", "DEBUG": "\033[92m", "WARNING": "\033[93m",
             "ERROR": "\033[91m", "SUCCESS": "\033[92m"}.get(level, "")
    print(f"{color}[{time.strftime('%H:%M:%S')}] {msg}\033[0m")

def extract_text(content) -> str:
    """从 content 中提取纯文本，支持字符串和列表"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content) if content else ""

# 剥离 <system-reminder>...</system-reminder> 等动态注入标签，只保留稳定部分用于 hash
_DYNAMIC_TAG_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")

def stable_system_text(system) -> str:
    """去掉动态标签，返回稳定文本用于 hash"""
    text = extract_text(system) if system else ""
    text = _DYNAMIC_TAG_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()

def sp_hash(system) -> str:
    return hashlib.md5(stable_system_text(system).encode()).hexdigest()[:12]

def history_hash(messages: list) -> str:
    key = json.dumps([
        {"role": m.get("role"), "content": extract_text(m.get("content", ""))}
        for m in messages
    ], ensure_ascii=False)
    return hashlib.md5(key.encode()).hexdigest()[:16]

def _create_thread_bg():
    """后台创建一个 thread 放入池"""
    import threading
    def _do():
        if not GLOBAL_AID:
            return
        try:
            r = requests.post(f"{UPSTREAM_URL}/assistants/{GLOBAL_AID}/threads",
                              headers=HEADERS, timeout=15)
            r.raise_for_status()
            tid = r.json()["thread_id"]
            with _pool_lock:
                _thread_pool.append(tid)
            log(f"🏊 pool+1 tid={tid} size={len(_thread_pool)}", "DEBUG")
        except Exception as e:
            log(f"⚠️ pool 补充失败: {e}", "WARNING")
    threading.Thread(target=_do, daemon=True).start()

def _get_pooled_thread() -> str | None:
    """从池里取一个空闲 thread，取出后立即异步补充"""
    with _pool_lock:
        if _thread_pool:
            tid = _thread_pool.pop(0)
            log(f"🏊 pool取出 tid={tid} 剩余={len(_thread_pool)}", "DEBUG")
            # 取出一个就补一个
            _create_thread_bg()
            return tid
    return None

def _register_tool_ids(tool_use_ids: list, tid: str):
    """记录 tool_use_id → thread_id，用于 tool_result 续接"""
    with _tool_tid_lock:
        for uid in tool_use_ids:
            _tool_tid_map[uid] = tid
        if len(_tool_tid_map) > 2000:
            keys = list(_tool_tid_map.keys())
            for k in keys[:1000]:
                del _tool_tid_map[k]

def _find_tool_result_thread(messages: list) -> str | None:
    """若最后一条消息是 tool_result（Claude 或 OpenAI 格式），返回对应 thread_id"""
    if not messages:
        return None
    last = messages[-1]
    role = last.get("role", "")

    # OpenAI 格式：role="tool", tool_call_id=...
    if role == "tool":
        uid = last.get("tool_call_id", "")
        if uid:
            with _tool_tid_lock:
                tid = _tool_tid_map.get(uid)
            if tid:
                log(f"🔗 tool_result(oai) → thread {tid} (id={uid[:16]})", "DEBUG")
                return tid

    # Claude 格式：role="user", content=[{type:"tool_result", tool_use_id:...}]
    if role == "user":
        content = last.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    uid = block.get("tool_use_id", "")
                    with _tool_tid_lock:
                        tid = _tool_tid_map.get(uid)
                    if tid:
                        log(f"🔗 tool_result → thread {tid} (id={uid[:16]})", "DEBUG")
                        return tid
    return None

def _format_tool_results(messages: list) -> str:
    """将 tool_result 消息格式化为模型能理解的文本（支持 Claude/OpenAI 格式）"""
    last = messages[-1]
    parts = []

    # OpenAI 格式：role="tool"
    if last.get("role") == "tool":
        parts.append(str(last.get("content", "")))
    else:
        # Claude 格式：content=[{type:"tool_result",...}]
        for block in (last.get("content") or []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            result = block.get("content", "")
            if isinstance(result, list):
                result = "".join(
                    b.get("text", "") for b in result
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            parts.append(str(result))

    combined = "\n---\n".join(parts) if parts else "[无结果]"
    return f"工具执行结果：\n{combined}\n\n请根据以上结果继续。"

def evict_expired_threads():
    now = time.time()
    expired = [k for k, v in thread_cache.items() if now - v["last_used"] > THREAD_TTL]
    for k in expired:
        del thread_cache[k]

# ===================== 线程管理（使用全局 assistant）=====================
def get_or_create_thread(messages: list, system_text: str = "") -> tuple[str, str]:
    """
    返回 (thread_id, message_to_send)。
    - 使用全局 GLOBAL_AID，完全消除 assistant 创建开销
    - system_text 前置到消息内容中（第一条消息时才加，避免重复）
    - 用前 N-1 条消息 hash 做 thread 复用 key
    """
    if not messages:
        raise ValueError("no messages")

    last_msg = messages[-1]
    history = messages[:-1]
    hh = history_hash(history) if history else "__empty__"
    cache_key = (hh,)

    user_text = extract_text(last_msg.get("content", ""))
    # 只在新对话第一条消息时把 system 前置进去
    if system_text and not history:
        user_text = f"<system>\n{system_text}\n</system>\n\n{user_text}"

    with lock:
        evict_expired_threads()
        entry = thread_cache.get(cache_key)
        if entry and time.time() - entry["last_used"] <= THREAD_TTL:
            tid = entry["thread_id"]
            entry["last_used"] = time.time()
            log(f"✅ 复用 thread: {tid}", "DEBUG")
            full_key = (history_hash(messages),)
            thread_cache[full_key] = {"thread_id": tid, "last_used": time.time()}
            return tid, user_text

    if not GLOBAL_AID:
        raise RuntimeError("全局 assistant 尚未初始化，请稍后重试")

    # 优先从池里取预热好的 thread
    pooled = _get_pooled_thread()
    if pooled:
        tid = pooled
        log(f"🏊 复用池 thread: {tid}", "INFO")
    else:
        log("🆕 新建 thread（池已空）...", "INFO")
        resp = requests.post(
            f"{UPSTREAM_URL}/assistants/{GLOBAL_AID}/threads",
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        tid = resp.json()["thread_id"]
        log(f"✅ Thread: {tid}", "SUCCESS")

    with lock:
        thread_cache[cache_key] = {"thread_id": tid, "last_used": time.time()}
        thread_cache[(history_hash(messages),)] = {"thread_id": tid, "last_used": time.time()}

    return tid, user_text


# ===================== Provider 路由 =====================
def resolve_provider(model: str) -> tuple[str, str]:
    if model.startswith("claude"):
        return "anthropic", model
    if model.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai", model
    if model.startswith("gemini"):
        return "google", model
    if model.startswith("mistral") or model.startswith("mixtral"):
        return "mistral", model
    if model.startswith("llama"):
        return "meta", model
    return "openai", model


def build_payload(text: str, stream: bool, model: str, tools=None) -> dict:
    provider, model_name = resolve_provider(model)
    p = {
        "content": text,
        "stream": stream,
        "llm_provider": provider,
        "model_name": model_name,
        "memory": "off",
    }
    log(f"🔀 {provider}/{model_name}", "DEBUG")
    return p


# ===================== 工具调用：prompt 注入方案 =====================
# 极简格式指令，不做角色扮演，不注入完整 schema
# 只告知输出格式 + 工具名列表，模型自行推断参数
_TOOL_PROMPT_TMPL = (
    "可用工具：{tools}\n"
    "需调用工具时仅输出JSON（无其他文字）：\n"
    '  单工具：{{"tool":"名","args":{{"参":值}}}}\n'
    '  多工具：{{"calls":[{{"tool":"名","args":{{}}}},...]}}\n'
    "不调工具则直接回答。\n\n"
    "{query}"
)

# prompt cache：按 tools_hash 缓存编译好的工具列表字符串
_tool_prompt_cache: dict[str, str] = {}

def _tools_hash(tools: list) -> str:
    key = json.dumps([t.get("name") or t.get("function", {}).get("name", "") for t in tools])
    return hashlib.md5(key.encode()).hexdigest()[:12]

def _compact_tools(tools: list) -> str:
    """只输出：name(req_param*,opt_param?) 格式，极简"""
    parts = []
    for t in tools:
        if t.get("type") == "function":
            fn = t["function"]
            name = fn.get("name", "")
            props = fn.get("parameters", {}).get("properties", {})
            req = set(fn.get("parameters", {}).get("required", []))
        elif "input_schema" in t:
            name = t.get("name", "")
            props = t.get("input_schema", {}).get("properties", {})
            req = set(t.get("input_schema", {}).get("required", []))
        else:
            continue
        params = [f"{p}{'*' if p in req else '?'}" for p in props]
        parts.append(f"{name}({','.join(params)})" if params else name)
    return " | ".join(parts)

def inject_tool_prompt(user_text: str, tools: list) -> str:
    """注入极简工具格式指令；用 cache 避免重复编译"""
    th = _tools_hash(tools)
    if th not in _tool_prompt_cache:
        _tool_prompt_cache[th] = _compact_tools(tools)
    tools_str = _tool_prompt_cache[th]
    result = _TOOL_PROMPT_TMPL.format(tools=tools_str, query=user_text)
    log(f"🔧 tool prompt {len(result)} chars (tools cached={th})", "DEBUG")
    return result


_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)

def parse_tool_response(raw: str) -> dict:
    """
    解析 LLM 输出，支持多种格式：
      {"tool":"name","args":{}}          单工具
      {"calls":[{"tool":"name","args":{}}]}   多工具
      {"type":"tool_calls","calls":[...]}     旧格式兼容
    """
    m = _JSON_RE.search(raw)
    if not m:
        return {"type": "text", "text": raw}
    try:
        obj = json.loads(m.group())
    except json.JSONDecodeError:
        return {"type": "text", "text": raw}

    # 单工具格式
    if "tool" in obj and "args" in obj:
        return {"type": "tool_calls", "calls": [{"name": obj["tool"], "input": obj["args"]}]}

    # 多工具格式（新/旧）
    calls_raw = obj.get("calls", [])
    if calls_raw:
        calls = []
        for c in calls_raw:
            name = c.get("tool") or c.get("name", "")
            inp = c.get("args") or c.get("parameters") or c.get("input") or {}
            calls.append({"name": name, "input": inp})
        return {"type": "tool_calls", "calls": calls}

    # 旧 type=text 格式
    if obj.get("type") == "text":
        return {"type": "text", "text": obj.get("content", raw)}

    return {"type": "text", "text": raw}


def tool_calls_to_claude_content(calls: list) -> list:
    """转为 Claude tool_use content blocks"""
    blocks = []
    for i, c in enumerate(calls):
        blocks.append({
            "type": "tool_use",
            "id": f"toolu_{int(time.time()*1000)}_{i}",
            "name": c["name"],
            "input": c["input"],
        })
    return blocks
    # 将工具调用格式化为 Claude 风格，让客户端执行（这里直接 pass-through）
    # 实际上我们无法在服务端执行客户端的工具，所以需要把 tool_use 事件透传给客户端
    # 这个函数保留用于将来服务端工具执行
    pass


# ===================== 模型列表 =====================
_models_cache: dict = {}
_models_cache_ts: float = 0
MODELS_CACHE_TTL = 3600

@app.route("/v1/models", methods=["GET"])
def list_models():
    global _models_cache, _models_cache_ts
    now = time.time()
    if _models_cache and now - _models_cache_ts < MODELS_CACHE_TTL:
        return jsonify(_models_cache)
    try:
        r = requests.get(
            f"{UPSTREAM_URL}/models",
            headers={"X-API-Key": API_KEY},
            params={"model_type": "llm", "limit": 500},
            timeout=15,
        )
        r.raise_for_status()
        upstream = r.json()
        data = [
            {"id": m["name"], "object": "model", "created": 0,
             "owned_by": m.get("provider", "unknown"),
             "context_window": m.get("context_limit", 0),
             "supports_tools": m.get("supports_tools", False)}
            for m in upstream.get("models", [])
        ]
        result = {"object": "list", "data": data}
        _models_cache = result
        _models_cache_ts = now
        return jsonify(result)
    except Exception as e:
        log(f"⚠️ 拉取模型列表失败: {e}", "WARNING")
        fallback = ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5-20251001",
                    "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
                    "gpt-4o", "gpt-4o-mini", "gemini-2.0-flash"]
        return jsonify({"object": "list", "data": [
            {"id": m, "object": "model", "created": 0, "owned_by": "backboard"} for m in fallback
        ]})

# ===================== 调试接口 =====================
@app.route("/debug/clear", methods=["POST"])
def debug_clear():
    with lock:
        thread_cache.clear()
    log("🧹 已清除 thread 缓存", "WARNING")
    return jsonify({"status": "ok"})

@app.route("/debug/state", methods=["GET"])
def debug_state():
    with lock:
        return jsonify({
            "global_assistant": GLOBAL_AID,
            "threads_cached": len(thread_cache),
            "pool_size": len(_thread_pool),
        })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "global_assistant": bool(GLOBAL_AID),
                    "threads_cached": len(thread_cache), "pool_size": len(_thread_pool)})


# ===================== 流式 SSE 生成器 =====================
def stream_claude(r, model: str, tid: str):
    """
    把上游 SSE 转为 Claude Messages API SSE 格式。
    支持工具调用：检测到 tool_use 时发出 tool_use content block。
    """
    msg_id = f"msg_{int(time.time()*1000)}"
    input_tokens = 0
    output_tokens = 0
    pending_tool: dict = {}   # 当前累积的 tool_use block
    tool_index = 0
    in_tool = False
    run_id = None

    yield f'data: {json.dumps({"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "model": model, "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})}\n\n'
    yield f'data: {json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})}\n\n'
    yield f'data: {json.dumps({"type": "ping"})}\n\n'

    text_block_open = True

    with r:
        if r.status_code >= 500:
            log(f"💥 上游500: {r.text[:100]}", "ERROR")
            yield f'data: {json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "[upstream error]"}})}\n\n'
        else:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    break
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                etype = obj.get("type", "")

                if etype == "run_started":
                    run_id = obj.get("run_id")

                elif etype == "content_streaming":
                    text = obj.get("content", "")
                    if text:
                        if not text_block_open:
                            # 之前关闭了 text block（比如进入 tool），重新开一个
                            yield f'data: {json.dumps({"type": "content_block_start", "index": tool_index + 1, "content_block": {"type": "text", "text": ""}})}\n\n'
                            text_block_open = True
                        yield f'data: {json.dumps({"type": "content_block_delta", "index": 0 if not in_tool else tool_index + 1, "delta": {"type": "text_delta", "text": text}})}\n\n'

                elif etype == "tool_call_start":
                    # 关闭当前文本 block
                    if text_block_open:
                        yield f'data: {json.dumps({"type": "content_block_stop", "index": 0})}\n\n'
                        text_block_open = False

                    tool_index = len(pending_tool) + 1
                    tc = obj.get("tool_call", {})
                    pending_tool = {
                        "id": tc.get("id", f"toolu_{int(time.time()*1000)}"),
                        "name": tc.get("function", {}).get("name", ""),
                        "input_buf": "",
                    }
                    in_tool = True
                    yield f'data: {json.dumps({"type": "content_block_start", "index": tool_index, "content_block": {"type": "tool_use", "id": pending_tool["id"], "name": pending_tool["name"], "input": {}}})}\n\n'

                elif etype == "tool_call_delta":
                    if in_tool and pending_tool:
                        delta_args = obj.get("tool_call", {}).get("function", {}).get("arguments", "")
                        pending_tool["input_buf"] += delta_args
                        yield f'data: {json.dumps({"type": "content_block_delta", "index": tool_index, "delta": {"type": "input_json_delta", "partial_json": delta_args}})}\n\n'

                elif etype == "tool_call_end":
                    if in_tool:
                        yield f'data: {json.dumps({"type": "content_block_stop", "index": tool_index})}\n\n'
                        in_tool = False

                elif etype == "run_ended":
                    input_tokens = obj.get("input_tokens", 0)
                    output_tokens = obj.get("output_tokens", 0)
                    break

    # 关闭尚未关闭的 block
    if text_block_open:
        yield f'data: {json.dumps({"type": "content_block_stop", "index": 0})}\n\n'

    stop_reason = "tool_use" if pending_tool and not in_tool else "end_turn"
    yield f'data: {json.dumps({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": output_tokens}})}\n\n'
    yield f'data: {json.dumps({"type": "message_stop"})}\n\n'


def stream_openai(r, model: str, cid: str):
    """把上游 SSE 转为 OpenAI chat completions SSE 格式，支持 function calling"""
    in_tool = False
    tool_index = -1
    pending_name = ""

    with r:
        if r.status_code >= 500:
            yield 'data: [DONE]\n\n'
            return
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            raw = line[6:]
            if raw == "[DONE]":
                break
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            etype = obj.get("type", "")

            if etype == "content_streaming":
                text = obj.get("content", "")
                if text:
                    yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})}\n\n'

            elif etype == "tool_call_start":
                tc = obj.get("tool_call", {})
                tool_index += 1
                pending_name = tc.get("function", {}).get("name", "")
                in_tool = True
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": tool_index, "id": tc.get("id", f"call_{tool_index}"), "type": "function", "function": {"name": pending_name, "arguments": ""}}]}, "finish_reason": None}]})}\n\n'

            elif etype == "tool_call_delta":
                if in_tool:
                    args = obj.get("tool_call", {}).get("function", {}).get("arguments", "")
                    yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": tool_index, "function": {"arguments": args}}]}, "finish_reason": None}]})}\n\n'

            elif etype == "tool_call_end":
                in_tool = False

            elif etype == "run_ended":
                break

    finish = "tool_calls" if tool_index >= 0 else "stop"
    yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})}\n\n'
    yield 'data: [DONE]\n\n'


# ===================== 核心接口 =====================
def _do_request(tid: str, text: str, stream: bool, model: str,
                client_tools: list) -> tuple[str, any]:
    """
    发消息到上游。有工具时强制用流式（避免大 prompt 非流式超时）。
    """
    if client_tools:
        text = inject_tool_prompt(text, client_tools)
        log(f"🔧 tools={len(client_tools)} 注入 prompt ({len(text)} chars)", "DEBUG")
        stream = True  # 工具请求强制流式，避免非流式超时

    payload = build_payload(text, stream, model)
    url = f"{UPSTREAM_URL}/threads/{tid}/messages"
    if stream:
        return url, requests.post(url, headers=HEADERS, json=payload, stream=True, timeout=120)
    return url, requests.post(url, headers=HEADERS, json=payload, timeout=90)


def _sync_response(up: dict, model: str, client_tools: list, tid: str = "") -> dict:
    """把上游非流式响应转为 Claude Messages API 格式，处理工具调用"""
    raw = up.get("content", "")

    if client_tools:
        parsed = parse_tool_response(raw)
        if parsed["type"] == "tool_calls":
            blocks = tool_calls_to_claude_content(parsed["calls"])
            if tid:
                _register_tool_ids([b["id"] for b in blocks], tid)
            log(f"🔧 工具调用: {[c['name'] for c in parsed['calls']]}", "INFO")
            return {
                "id": f"msg_{int(time.time()*1000)}",
                "type": "message", "role": "assistant",
                "content": blocks, "model": model,
                "stop_reason": "tool_use",
                "usage": {"input_tokens": up.get("input_tokens", 0),
                          "output_tokens": up.get("output_tokens", 0)},
            }
        raw = parsed.get("text", raw)

    return {
        "id": f"msg_{int(time.time()*1000)}",
        "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": raw}],
        "model": model, "stop_reason": "end_turn",
        "usage": {"input_tokens": up.get("input_tokens", 0),
                  "output_tokens": up.get("output_tokens", 0)},
    }


def stream_claude_with_tools(r, model: str, client_tools: bool, tid: str = ""):
    """
    有工具时：缓冲全部响应，解析 JSON，转为 SSE tool_use 事件。
    无工具时：直接流式透传。
    """
    if not client_tools:
        yield from stream_claude(r, model, "")
        return

    # 缓冲收集完整响应文本
    msg_id = f"msg_{int(time.time()*1000)}"
    buf = []
    input_tokens = 0
    output_tokens = 0

    with r:
        if r.status_code >= 500:
            yield f'data: {json.dumps({"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "model": model, "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})}\n\n'
            yield f'data: {json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})}\n\n'
            yield f'data: {json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "[upstream error]"}})}\n\n'
            yield f'data: {json.dumps({"type": "content_block_stop", "index": 0})}\n\n'
            yield f'data: {json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 0}})}\n\n'
            yield f'data: {json.dumps({"type": "message_stop"})}\n\n'
            return
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            raw = line[6:]
            if raw == "[DONE]":
                break
            try:
                obj = json.loads(raw)
                if obj.get("type") == "content_streaming":
                    buf.append(obj.get("content", ""))
                elif obj.get("type") == "run_ended":
                    input_tokens = obj.get("input_tokens", 0)
                    output_tokens = obj.get("output_tokens", 0)
                    break
            except Exception:
                pass

    full_text = "".join(buf)
    parsed = parse_tool_response(full_text)

    yield f'data: {json.dumps({"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "model": model, "stop_reason": None, "usage": {"input_tokens": input_tokens, "output_tokens": 0}}})}\n\n'

    if parsed["type"] == "tool_calls":
        log(f"🔧 流式工具调用: {[c['name'] for c in parsed['calls']]}", "INFO")
        tool_ids = []
        for i, call in enumerate(parsed["calls"]):
            tid_val = f"toolu_{int(time.time()*1000)}_{i}"
            tool_ids.append(tid_val)
            inp_str = json.dumps(call["input"], ensure_ascii=False)
            yield f'data: {json.dumps({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": tid_val, "name": call["name"], "input": {}}})}\n\n'
            yield f'data: {json.dumps({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": inp_str}})}\n\n'
            yield f'data: {json.dumps({"type": "content_block_stop", "index": i})}\n\n'
        if tid and tool_ids:
            _register_tool_ids(tool_ids, tid)
        yield f'data: {json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": output_tokens}})}\n\n'
    else:
        text = parsed.get("text", full_text)
        yield f'data: {json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})}\n\n'
        yield f'data: {json.dumps({"type": "ping"})}\n\n'
        yield f'data: {json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})}\n\n'
        yield f'data: {json.dumps({"type": "content_block_stop", "index": 0})}\n\n'
        yield f'data: {json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": output_tokens}})}\n\n'

    yield f'data: {json.dumps({"type": "message_stop"})}\n\n'


def _buffer_upstream_stream(r) -> tuple[str, int, int]:
    """缓冲上游流式响应，返回 (full_text, input_tokens, output_tokens)"""
    buf = []
    input_tokens = output_tokens = 0
    with r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            raw = line[6:]
            if raw == "[DONE]":
                break
            try:
                obj = json.loads(raw)
                if obj.get("type") == "content_streaming":
                    buf.append(obj.get("content", ""))
                elif obj.get("type") == "run_ended":
                    input_tokens = obj.get("input_tokens", 0)
                    output_tokens = obj.get("output_tokens", 0)
                    break
            except Exception:
                pass
    return "".join(buf), input_tokens, output_tokens


@app.route("/v1/messages", methods=["POST"])
def claude_messages():
    data = request.json
    model = data.get("model", "claude-sonnet-4-6")
    messages = data.get("messages", [])
    system = data.get("system") or "You are a helpful assistant."
    stream = data.get("stream", False)
    client_tools = data.get("tools", [])

    log(f"📥 /v1/messages model={model} stream={stream} msgs={len(messages)} tools={len(client_tools)}", "INFO")
    if not messages:
        return jsonify({"error": "no messages"}), 400

    try:
        sys_text = stable_system_text(system)
        # 检测是否为 tool_result 续接轮次
        tool_result_tid = _find_tool_result_thread(messages)
        if tool_result_tid:
            tid = tool_result_tid
            text = _format_tool_results(messages)
            log(f"🔄 tool_result 续接 thread={tid}", "INFO")
        else:
            tid, text = get_or_create_thread(messages, sys_text)
        _, resp = _do_request(tid, text, stream, model, client_tools)
        # 有工具时上游强制流式；无工具时跟随客户端
        upstream_streaming = bool(client_tools) or stream

        if not stream:
            if upstream_streaming:
                # 工具场景：缓冲流式响应后返回同步
                if resp.status_code >= 500:
                    log(f"💥 上游500", "ERROR")
                    return jsonify({"error": "upstream error"}), 502
                full_text, in_tok, out_tok = _buffer_upstream_stream(resp)
                fake_up = {"content": full_text, "input_tokens": in_tok, "output_tokens": out_tok}
                return jsonify(_sync_response(fake_up, model, client_tools, tid))
            else:
                if resp.status_code >= 500:
                    log(f"💥 上游500: {resp.text[:200]}", "ERROR")
                    return jsonify({"error": "upstream error"}), 502
                resp.raise_for_status()
                return jsonify(_sync_response(resp.json(), model, client_tools, tid))

        return Response(stream_claude_with_tools(resp, model, bool(client_tools), tid),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    except Exception as e:
        log(f"❌ 接口异常: {e}", "ERROR")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 502


@app.route("/v1/chat/completions", methods=["POST"])
def openai_compat():
    data = request.json
    messages = data.get("messages", [])
    stream = data.get("stream", False)
    model = data.get("model", "gpt-4o")
    client_tools = data.get("tools", [])
    system_msg = next((extract_text(m["content"]) for m in messages if m.get("role") == "system"),
                      "You are a helpful assistant.")
    non_system = [m for m in messages if m.get("role") != "system"]

    log(f"📥 /v1/chat/completions model={model} stream={stream} msgs={len(non_system)} tools={len(client_tools)}", "INFO")
    if not non_system:
        return jsonify({"error": "no messages"}), 400

    try:
        sys_text = stable_system_text(system_msg)
        tool_result_tid = _find_tool_result_thread(non_system)
        if tool_result_tid:
            tid = tool_result_tid
            text = _format_tool_results(non_system)
            log(f"🔄 tool_result 续接 thread={tid}", "INFO")
        else:
            tid, text = get_or_create_thread(non_system, sys_text)
        _, resp = _do_request(tid, text, stream, model, client_tools)
        upstream_streaming = bool(client_tools) or stream

        if not stream:
            if upstream_streaming:
                if resp.status_code >= 500:
                    return jsonify({"error": "upstream error"}), 502
                full_text, in_tok, out_tok = _buffer_upstream_stream(resp)
            else:
                if resp.status_code >= 500:
                    return jsonify({"error": "upstream error"}), 502
                resp.raise_for_status()
                up = resp.json()
                full_text = up.get("content", "")
                in_tok, out_tok = up.get("input_tokens", 0), up.get("output_tokens", 0)

            if client_tools:
                parsed = parse_tool_response(full_text)
                if parsed["type"] == "tool_calls":
                    oai_tc = [
                        {"id": f"call_{i}", "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": json.dumps(c["input"], ensure_ascii=False)}}
                        for i, c in enumerate(parsed["calls"])
                    ]
                    _register_tool_ids([tc["id"] for tc in oai_tc], tid)
                    return jsonify({
                        "id": f"chatcmpl-{int(time.time()*1000)}",
                        "object": "chat.completion",
                        "choices": [{"index": 0,
                                     "message": {"role": "assistant", "content": None,
                                                 "tool_calls": oai_tc},
                                     "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok},
                    })
                full_text = parsed.get("text", full_text)

            return jsonify({
                "id": f"chatcmpl-{int(time.time()*1000)}",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": full_text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok},
            })

        def gen_oai(r):
            cid = f"chatcmpl-{int(time.time()*1000)}"
            if not client_tools:
                yield from stream_openai(r, model, cid)
                return
            full_text2, _, _ = _buffer_upstream_stream(r)
            parsed2 = parse_tool_response(full_text2)
            if parsed2["type"] == "tool_calls":
                oai_tc2 = [
                    {"id": f"call_{i}", "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": json.dumps(c["input"], ensure_ascii=False)}}
                    for i, c in enumerate(parsed2["calls"])
                ]
                _register_tool_ids([tc["id"] for tc in oai_tc2], tid)
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": oai_tc2}, "finish_reason": None}]})}\n\n'
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})}\n\n'
            else:
                t2 = parsed2.get("text", full_text2)
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": t2}, "finish_reason": None}]})}\n\n'
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}\n\n'
            yield 'data: [DONE]\n\n'

        return Response(gen_oai(resp), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    except Exception as e:
        log(f"❌ 接口异常: {e}", "ERROR")
        return jsonify({"error": str(e)}), 502


# ===================== 预热 =====================
def warmup():
    """启动时创建全局 assistant + 填满 thread 池"""
    import threading
    def _do():
        global GLOBAL_AID
        try:
            log("🔥 预热...", "INFO")
            resp = requests.post(
                f"{UPSTREAM_URL}/assistants",
                headers=HEADERS,
                json={"name": "proxy-global", "system_prompt": "You are a helpful assistant."},
                timeout=15,
            )
            resp.raise_for_status()
            GLOBAL_AID = resp.json()["assistant_id"]
            log(f"✅ 全局 assistant: {GLOBAL_AID}", "SUCCESS")

            # 并行创建 THREAD_POOL_SIZE 个 thread
            import concurrent.futures
            def make_thread(_):
                r = requests.post(f"{UPSTREAM_URL}/assistants/{GLOBAL_AID}/threads",
                                  headers=HEADERS, timeout=15)
                r.raise_for_status()
                return r.json()["thread_id"]

            with concurrent.futures.ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE) as ex:
                tids = list(ex.map(make_thread, range(THREAD_POOL_SIZE)))
            with _pool_lock:
                _thread_pool.extend(tids)
            log(f"✅ 预热完成: pool={len(_thread_pool)} threads", "SUCCESS")
        except Exception as e:
            log(f"⚠️ 预热失败: {e}", "WARNING")
    threading.Thread(target=_do, daemon=True).start()


# ===================== 启动 =====================
if __name__ == "__main__":
    print("\n" + "="*60)
    print("🚀 ccc.py 启动")
    print(f"📍 端口: {PORT}  上游: {UPSTREAM_URL}")
    print(f"⏱️  Thread TTL: {THREAD_TTL}s")
    print("="*60 + "\n")
    warmup()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
