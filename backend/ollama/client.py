import json
import inspect
import logging
import re
from typing import Any

import requests

from backend.config.settings import Settings

logger = logging.getLogger(__name__)

# Ensure tools are registered when this module is imported
import backend.ollama.tools  # noqa: F401


# ------------------------------------------------------------------
# Tool registry
# ------------------------------------------------------------------

TOOL_FUNCTIONS: dict[str, callable] = {}


def register_tool(name: str):
    """Decorator to register a function as a tool."""
    def decorator(fn):
        TOOL_FUNCTIONS[name] = fn
        return fn
    return decorator


def run_tool(name: str, args: dict) -> Any:
    """Execute a registered tool and return its result."""
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args)
    except Exception as e:
        return {"error": f"Tool '{name}' failed: {e}"}


def get_tool_definitions() -> list[dict]:
    """Return all registered tools in OpenAI function-calling format."""
    tools = []
    for name, fn in TOOL_FUNCTIONS.items():
        sig = inspect.signature(fn)
        params = {}
        required = []
        for param_name, param in sig.parameters.items():
            param_type = "string"
            if param.annotation is int:
                param_type = "integer"
            elif param.annotation is float:
                param_type = "number"
            elif param.annotation is bool:
                param_type = "boolean"
            params[param_name] = {"type": param_type, "description": param_name}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": (fn.__doc__ or name).strip(),
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "required": required,
                },
            },
        })
    return tools


# ------------------------------------------------------------------
# Ollama client
# ------------------------------------------------------------------

class OllamaClient:
    """Client for the native Ollama /api/chat endpoint.

    The native endpoint (unlike the OpenAI-compatible /v1/chat/completions)
    honors the `options` field, which is the only way to set num_ctx per
    request — essential for keeping context predictable on small hardware.
    """

    def __init__(self, settings: Settings):
        self.base_url = settings.ollama_url.rstrip("/")
        self.model = settings.ollama_model
        self.num_ctx = getattr(settings, "ollama_num_ctx", 4096)
        self.session = requests.Session()

    def _chat(self, messages: list[dict], tools: list[dict] | None = None, stream: bool = False):
        """Call /api/chat. Returns the message dict (non-stream) or the Response (stream)."""
        url = f"{self.base_url}/api/chat"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": 0.2, "num_ctx": self.num_ctx},
        }
        if tools:
            payload["tools"] = tools

        if stream:
            resp = self.session.post(url, json=payload, stream=True, timeout=(15, None))
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return resp
        resp = self.session.post(url, json=payload, timeout=300)
        resp.raise_for_status()
        return resp.json().get("message", {})

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        """Send messages to the LLM with tools, loop through tool_calls, return final answer.

        Supports native Ollama tool_calls plus text-based tool call formats
        that small models (Gemma, Llama, Mistral) sometimes fall back to.
        """
        history = list(messages)
        max_rounds = 5

        for round_num in range(max_rounds):
            msg = self._chat(history, tools=tools, stream=False)
            content = msg.get("content", "") or ""

            logger.debug(f"LLM round {round_num}: content={content[:200]!r}, tool_calls={msg.get('tool_calls')}")

            history.append(msg)

            tool_calls = self._extract_native_tool_calls(msg)
            if not tool_calls:
                tool_calls = self._parse_text_tool_calls(content)

            if not tool_calls:
                # No tool calls — this is the final answer
                return self._parse_content(content)

            logger.info(f"Executing tool calls: {[tc.get('name') for tc in tool_calls]}")
            for tc in tool_calls:
                history.append(self._run_tool_call(tc))

        content = history[-1].get("content", "") if history else ""
        return self._parse_content(content)

    def chat_stream_events(self, messages: list[dict], tools: list[dict] | None = None):
        """Send messages to the LLM with tools, yield events as they happen, handle tool loop."""
        history = list(messages)
        max_rounds = 5

        for round_num in range(max_rounds):
            resp = self._chat(history, tools=tools, stream=True)

            content = ""
            raw_tool_calls: list[dict] = []

            for raw in resp.iter_lines(decode_unicode=False):
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_part = data.get("message", {})
                if msg_part.get("tool_calls"):
                    raw_tool_calls.extend(msg_part["tool_calls"])
                chunk = msg_part.get("content", "")
                if chunk:
                    content += chunk
                    yield {"type": "content_chunk", "text": chunk}
                if data.get("done"):
                    break

            msg: dict[str, Any] = {"role": "assistant", "content": content}
            if raw_tool_calls:
                msg["tool_calls"] = raw_tool_calls
            history.append(msg)

            tool_calls = self._extract_native_tool_calls(msg)
            if not tool_calls:
                tool_calls = self._parse_text_tool_calls(content)

            if not tool_calls:
                yield {"type": "final_answer", "content": content}
                return

            for tc in tool_calls:
                yield {"type": "tool_call", "name": tc.get("name"), "args": tc.get("args")}

            for tc in tool_calls:
                tool_message = self._run_tool_call(tc)
                history.append(tool_message)
                yield {"type": "tool_result", "name": tc.get("name"), "result": tool_message["content"]}

        content = history[-1].get("content", "") if history else ""
        yield {"type": "final_answer", "content": content}

    @staticmethod
    def _run_tool_call(tc: dict) -> dict:
        """Execute one tool call and return the tool-role message for the history."""
        tool_name = tc.get("name", "")
        tool_args = tc.get("args", {})
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except json.JSONDecodeError:
                tool_args = {}

        result = run_tool(tool_name, tool_args)
        return {
            "role": "tool",
            "tool_name": tool_name,
            "content": OllamaClient._format_tool_result(tool_name, result),
        }

    @staticmethod
    def _extract_native_tool_calls(msg: dict) -> list[dict]:
        """Extract tool calls from an Ollama message. Arguments may be a dict or a JSON string."""
        raw_calls = msg.get("tool_calls")
        if not raw_calls:
            return []
        result = []
        for i, tc in enumerate(raw_calls):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {})
            if isinstance(fn, str):
                try:
                    fn = json.loads(fn)
                except json.JSONDecodeError:
                    fn = {}
            name = fn.get("name", "") or tc.get("name", "")
            if not name:
                continue
            args_raw = fn.get("arguments", {}) or tc.get("args", {})
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw or {}
            result.append({
                "name": name,
                "args": args,
                "id": tc.get("id", f"call_{i}"),
            })
        return result

    @staticmethod
    def _parse_text_tool_calls(content: str) -> list[dict]:
        """Parse tool calls from free-text content. Handles multiple formats."""
        if not content:
            return []

        tool_calls = []

        # Format 1: XML-style <tool_call>...</tool_call>
        tool_calls.extend(
            OllamaClient._parse_xml_tool_calls(content)
        )

        # Format 2: Function-call style: function_name(args) on its own line
        if not tool_calls:
            tool_calls.extend(
                OllamaClient._parse_function_call_style(content)
            )

        # Format 3: JSON tool call: {"name": "...", "arguments": {...}}
        if not tool_calls:
            tool_calls.extend(
                OllamaClient._parse_json_tool_calls(content)
            )

        # Format 4: Tool-style: Tool: name\nAction Input: {...}
        if not tool_calls:
            tool_calls.extend(
                OllamaClient._parse_action_input_style(content)
            )

        return tool_calls

    @staticmethod
    def _parse_xml_tool_calls(content: str) -> list[dict]:
        """Parse <tool_call>name(args)</tool_call> and <tool_call>{"name":...,"args":...}</tool_call>."""
        tool_calls = []
        for match in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", content, re.DOTALL):
            inner = match.group(1).strip()
            if not inner:
                continue

            # Try: {"name": "...", "arguments": {...}}  (JSON object)
            json_match = re.match(r"\{.*\}", inner, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(0))
                    name = data.get("name", "")
                    args = data.get("arguments") or data.get("args") or data.get("parameters") or {}
                    if name and isinstance(args, dict):
                        tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
                        continue
                except json.JSONDecodeError:
                    pass

            # Try: name(args) — function call style
            fn_match = re.match(r"(\w[\w_]*)\s*\((.*)\)", inner, re.DOTALL)
            if fn_match:
                name = fn_match.group(1)
                args_str = fn_match.group(2).strip()
                args = {}
                if args_str:
                    # Try JSON parse of the args
                    try:
                        args = json.loads(args_str)
                    except json.JSONDecodeError:
                        # Try wrapping in braces
                        try:
                            args = json.loads("{" + args_str + "}")
                        except json.JSONDecodeError:
                            args = {}
                tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
                continue

            # Plain name with no args
            if re.match(r"^[\w_]+$", inner):
                tool_calls.append({"name": inner, "args": {}, "id": f"call_{len(tool_calls)}"})

        return tool_calls

    @staticmethod
    def _parse_function_call_style(content: str) -> list[dict]:
        """Parse lines like: function_name({"key": "value"}) or function_name()"""
        tool_calls = []
        for match in re.finditer(
            r"^(\w[\w_]*)\s*\((.*?)\)\s*$", content, re.MULTILINE | re.DOTALL
        ):
            name = match.group(1)
            args_str = match.group(2).strip()
            args = {}
            if args_str:
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    try:
                        args = json.loads("{" + args_str + "}")
                    except json.JSONDecodeError:
                        args = {}
            tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
        return tool_calls

    @staticmethod
    def _parse_json_tool_calls(content: str) -> list[dict]:
        """Parse standalone JSON objects that look like tool calls: {"name": "...", "arguments": {...}}"""
        tool_calls = []
        # Look for JSON objects with "name" and "arguments" keys
        for match in re.finditer(r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}', content, re.DOTALL):
            name = match.group(1)
            try:
                args = json.loads(match.group(2))
                tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
            except json.JSONDecodeError:
                tool_calls.append({"name": name, "args": {}, "id": f"call_{len(tool_calls)}"})
        return tool_calls

    @staticmethod
    def _parse_action_input_style(content: str) -> list[dict]:
        """Parse ReAct-style: Tool: name\nAction Input: {...}"""
        tool_calls = []
        for match in re.finditer(
            r"Tool:\s*(\w[\w_]*)\s*\n\s*Action\s*Input:\s*(.*)",
            content,
            re.IGNORECASE | re.DOTALL,
        ):
            name = match.group(1)
            input_str = match.group(2).strip()
            args = {}
            if input_str:
                try:
                    args = json.loads(input_str)
                except json.JSONDecodeError:
                    args = {"query": input_str}
            tool_calls.append({"name": name, "args": args, "id": f"call_{len(tool_calls)}"})
        return tool_calls

    @staticmethod
    def _format_tool_result(tool_name: str, result: Any) -> str:
        """Format a tool result into a concise summary for the LLM."""
        if isinstance(result, dict):
            if "error" in result:
                return json.dumps(result)
            if tool_name == "scan_containers":
                containers = result.get("containers", [])
                count = result.get("count", len(containers))
                running = [c for c in containers if c.get("status") == "running"]
                stopped = [c for c in containers if c.get("status") == "stopped"]
                lines = [f"Container scan: {count} total ({len(running)} running, {len(stopped)} stopped)"]
                for c in containers:
                    ip_info = f", IP: {c.get('ip')}" if c.get("ip") else ""
                    vmid = c.get('vmid', 'unknown')
                    lines.append(f"  - {c.get('name', 'unknown')} (vmid: {vmid}, {c.get('type', 'unknown')}, {c.get('node', 'unknown')}, {c.get('status', 'unknown')}{ip_info})")
                return "\n".join(lines)
            if tool_name == "get_logs":
                logs = result.get("logs", [])
                container = result.get("container", "all")
                lines = [f"Logs for {container}: {len(logs)} entries"]
                for log in logs[:10]:
                    msg = str(log.get("message", ""))[:120]
                    lines.append(f"  - {msg}")
                return "\n".join(lines)
        # Default: truncate large JSON
        text = json.dumps(result) if not isinstance(result, str) else result
        if len(text) > 2000:
            return text[:2000] + "...(truncated)"
        return text

    @staticmethod
    def _parse_content(content: str) -> dict[str, Any]:
        """Try to parse the assistant content as JSON, fall back to raw text."""
        if not content:
            return {"summary": "No response from model.", "reasoning": "", "confidence": 0.0}

        cleaned = content.strip()

        # Try direct JSON parse first
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown code fences
        fence_match = re.search(r"```(?:json)?\s*\n?(.+?)\n?```", cleaned, re.DOTALL)
        if fence_match:
            try:
                parsed = json.loads(fence_match.group(1).strip())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        # Try to find JSON objects with nested braces (iteratively)
        for pattern in [
            r"\{[^{}]*\}",                          # simple: {"key": "value"}
            r"\{[^{}]*\{[^{}]*\}[^{}]*\}",          # one level nested
            r"\{[^{}]*\{[^{}]*\{[^{}]*\}[^{}]*\}[^{}]*\}",  # two levels
        ]:
            for json_match in re.finditer(pattern, cleaned, re.DOTALL):
                try:
                    parsed = json.loads(json_match.group(0))
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue

        # Fall back: wrap the text in the expected schema
        return {
            "summary": cleaned,
            "reasoning": "Model returned text instead of JSON.",
            "confidence": 0.3,
            "suggested_actions": [],
        }

    def list_models(self) -> list[str]:
        """Return installed Ollama model names."""
        url = f"{self.base_url}/api/tags"
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException:
            return []
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
