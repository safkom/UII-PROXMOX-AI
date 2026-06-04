from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import requests
import streamlit as st

DEFAULT_BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")

st.set_page_config(page_title="AI Homelab Assistant", page_icon="🧠", layout="wide")


def get_backend_url() -> str:
    return st.session_state.get("backend_url", DEFAULT_BACKEND_URL)


def backend_get(path: str) -> Any:
    response = requests.get(f"{get_backend_url()}{path}", timeout=20)
    response.raise_for_status()
    return response.json()


def backend_post(path: str, payload: dict[str, Any]) -> requests.Response:
    response = requests.post(f"{get_backend_url()}{path}", json=payload, timeout=30)
    response.raise_for_status()
    return response


def backend_patch(path: str, payload: dict[str, Any]) -> requests.Response:
    response = requests.patch(f"{get_backend_url()}{path}", json=payload, timeout=30)
    response.raise_for_status()
    return response


def backend_execute(approval_id: str) -> dict[str, Any]:
    response = requests.post(
        f"{get_backend_url()}/execute",
        json={"approval_id": approval_id},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def stream_chat(query: str, model: str | None) -> dict[str, Any]:
    payload = {
        "query": query,
        "include_logs": False,
        "log_limit": 20,
        "model": model or None,
    }
    response = requests.post(
        f"{get_backend_url()}/chat/stream",
        json=payload,
        stream=True,
        timeout=(20, None),
    )
    response.raise_for_status()
    response.encoding = "utf-8"

    assistant_box = st.chat_message("assistant")
    text_slot = assistant_box.empty()
    status_slot = assistant_box.empty()

    accumulated = ""
    final_payload: dict[str, Any] | None = None
    buffer = ""

    def to_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return value
        return str(value)

    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        buffer += to_text(raw_line) + "\n"
        lines = buffer.splitlines(keepends=False)
        if buffer and not buffer.endswith("\n"):
            buffer = lines.pop() if lines else buffer
        else:
            buffer = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "chunk":
                piece = event.get("text", "")
                if isinstance(piece, str) and piece:
                    accumulated += piece
                    text_slot.markdown(accumulated)
                    status_slot.caption("Streaming response from the local model...")
            elif event.get("type") == "final":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    final_payload = payload
            elif event.get("type") == "error":
                raise RuntimeError(str(event.get("error", "Unknown streaming error")))
            elif event.get("type") == "tool_call":
                # Store pending tool calls in session state so UI can render Accept/Deny
                tc = event.get("args", {}) or {}
                tool = event.get("tool") or "unknown"
                pending = st.session_state.get("pending_calls", [])
                call_id = f"tc-{len(pending)}-{int(datetime.now(timezone.utc).timestamp())}"
                pending.append({"id": call_id, "tool": tool, "args": tc, "source_query": query})
                st.session_state.pending_calls = pending
                # show a short caption in the assistant bubble
                text_slot.markdown(accumulated + "\n\n*Tool call received — awaiting approval...*")

    if final_payload is None:
        final_payload = {
            "summary": accumulated,
            "reasoning": "",
            "confidence": 0.0,
            "suggested_actions": [],
        }

    if final_payload.get("summary"):
        text_slot.markdown(str(final_payload["summary"]))
    status_slot.caption("Response complete.")
    # persist the final payload in session state so UI reruns keep suggested actions
    st.session_state["last_final_payload"] = final_payload
    return final_payload


if "messages" not in st.session_state:
    st.session_state.messages = []
if "approvals" not in st.session_state:
    st.session_state.approvals = []
if "pending_calls" not in st.session_state:
    st.session_state.pending_calls = []
if "models" not in st.session_state:
    st.session_state.models = []
if "selected_model" not in st.session_state:
    st.session_state.selected_model = None
if "backend_url" not in st.session_state:
    st.session_state.backend_url = DEFAULT_BACKEND_URL
if "last_final_payload" not in st.session_state:
    st.session_state.last_final_payload = {}

st.title("AI Homelab Assistant")
st.caption("Streamed chat, inline approvals, and command execution against your local backend.")

with st.sidebar:
    st.subheader("Connection")
    backend_url_input = st.text_input("Backend URL", value=st.session_state.backend_url)
    if backend_url_input:
        st.session_state.backend_url = backend_url_input.rstrip("/")
    st.caption("This should be the FastAPI app that exposes /chat, /approvals, /models, and /execute.")
    if st.button("Refresh models"):
        try:
            st.session_state.models = backend_get("/models")
        except Exception as exc:
            st.error(f"Failed to load models: {exc}")
    if not st.session_state.models:
        try:
            st.session_state.models = backend_get("/models")
        except Exception:
            st.session_state.models = []

    if st.session_state.models and st.session_state.selected_model not in st.session_state.models:
        st.session_state.selected_model = st.session_state.models[0]

    st.session_state.selected_model = st.selectbox(
        "Model",
        options=st.session_state.models if st.session_state.models else [""],
        index=0 if not st.session_state.models or st.session_state.selected_model not in st.session_state.models else st.session_state.models.index(st.session_state.selected_model),
        format_func=lambda value: value or "No models available",
    )

    if st.button("Refresh approvals"):
        try:
            st.session_state.approvals = backend_get("/approvals")
        except Exception as exc:
            st.error(f"Failed to load approvals: {exc}")

    if not st.session_state.approvals:
        try:
            st.session_state.approvals = backend_get("/approvals")
        except Exception:
            st.session_state.approvals = []

st.subheader("Chat")
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

def render_suggested_actions(payload: dict[str, Any], user_query: str | None = None) -> None:
    suggested_actions = payload.get("suggested_actions", [])
    if isinstance(suggested_actions, list) and suggested_actions:
        st.subheader("Suggested actions")
        for action in suggested_actions:
            if not isinstance(action, dict):
                continue

            action_title = str(action.get("action", "Suggested action"))
            command = action.get("command")
            target = action.get("target")
            risk = str(action.get("risk", "medium"))

            with st.container(border=True):
                st.markdown(f"**{action_title}**")
                st.markdown(f"Risk: `{risk}`")
                if target:
                    st.markdown(f"Target: `{target}`")
                if command:
                    st.code(str(command), language="bash")
                else:
                    st.caption("No command was suggested.")

                col1, col2 = st.columns(2)
                if command:
                    with col1:
                        if st.button("Accept", key=f"accept-{action_title}-{command}"):
                            try:
                                result = backend_post(
                                    "/execute/direct",
                                    {"command": command, "target": target, "timeout": 60},
                                ).json()
                                st.session_state.messages.append({"role": "assistant", "content": json.dumps(result, indent=2)})
                                st.session_state.last_execution_result = result
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Execution failed: {exc}")
                    with col2:
                        if st.button("Deny", key=f"deny-{action_title}-{command}"):
                            st.session_state.last_action_dismissed = {"action": action_title, "command": command}
                            st.rerun()

    if st.session_state.get("pending_calls"):
        st.subheader("Pending tool calls")
        for call in list(st.session_state.pending_calls):
            with st.container(border=True):
                st.markdown(f"**Tool call: {call.get('tool')}**")
                st.code(json.dumps(call.get('args', {}), indent=2), language="json")
                c1, c2 = st.columns(2)
                if c1.button("Accept", key=f"accept-pc-{call['id']}"):
                    try:
                        args = call.get('args', {}) or {}
                        cmd = args.get('command') or args.get('cmd')
                        if isinstance(cmd, list):
                            cmd = ' '.join(map(str, cmd))
                        if not cmd:
                            st.error("No executable command found in tool_call")
                        else:
                            res = backend_post('/execute/direct', {"command": cmd, "target": args.get('target'), "timeout": 60}).json()
                            st.session_state.messages.append({"role": "assistant", "content": json.dumps(res, indent=2)})
                            st.session_state.last_execution_result = res
                            st.session_state.pending_calls = [p for p in st.session_state.pending_calls if p['id'] != call['id']]
                            st.rerun()
                    except Exception as exc:
                        st.error(f"Execution failed: {exc}")
                if c2.button("Deny", key=f"deny-pc-{call['id']}"):
                    st.session_state.pending_calls = [p for p in st.session_state.pending_calls if p['id'] != call['id']]
                    st.rerun()

    st.subheader("Approvals")
    approvals = st.session_state.approvals or []
    if not approvals:
        st.info("No approvals yet.")
    for approval in approvals:
        if not isinstance(approval, dict):
            continue
        with st.container(border=True):
            st.markdown(f"**{approval.get('action', 'Approval request')}**")
            st.markdown(f"Status: `{approval.get('status', '')}` | Risk: `{approval.get('risk', '')}`")
            if approval.get("requested_by"):
                st.markdown(f"Requested by: `{approval['requested_by']}`")
            if approval.get("source_query"):
                st.markdown(f"Source: {approval['source_query']}")
            if approval.get("command"):
                st.code(str(approval["command"]), language="bash")

            cols = st.columns(3)
            if approval.get("status") == "pending":
                with cols[0]:
                    if st.button("Approve", key=f"approve-card-{approval['id']}"):
                        try:
                            updated = backend_patch(
                                f"/approvals/{approval['id']}",
                                {"decision": "approved", "reviewer": "streamlit", "note": "approved from Streamlit"},
                            ).json()
                            st.session_state.approvals = [updated if a.get("id") == updated.get("id") else a for a in st.session_state.approvals]
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Failed to approve: {exc}")
                with cols[1]:
                    if st.button("Reject", key=f"reject-card-{approval['id']}"):
                        try:
                            updated = backend_patch(
                                f"/approvals/{approval['id']}",
                                {"decision": "rejected", "reviewer": "streamlit", "note": "rejected from Streamlit"},
                            ).json()
                            st.session_state.approvals = [updated if a.get("id") == updated.get("id") else a for a in st.session_state.approvals]
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Failed to reject: {exc}")
                with cols[2]:
                    if st.button("Dismiss", key=f"dismiss-card-{approval['id']}"):
                        try:
                            resp = requests.delete(f"{get_backend_url()}/approvals/{approval['id']}")
                            resp.raise_for_status()
                            st.session_state.approvals = backend_get('/approvals')
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Failed to dismiss: {exc}")
            if approval.get("status") == "approved" and approval.get("command"):
                with cols[2]:
                    if st.button("Execute", key=f"execute-card-{approval['id']}"):
                        try:
                            result = backend_execute(str(approval["id"]))
                            st.code(json.dumps(result, indent=2), language="json")
                        except Exception as exc:
                            st.error(f"Execution failed: {exc}")

user_query = st.chat_input("Ask about the homelab, logs, or available actions...")
if user_query:
    st.session_state.messages.append({"role": "user", "content": user_query})
    with st.chat_message("user"):
        st.markdown(user_query)

    try:
        final_payload = stream_chat(user_query, st.session_state.selected_model)
        assistant_summary = str(final_payload.get("summary", ""))
        st.session_state.messages.append({"role": "assistant", "content": assistant_summary})

        st.session_state.last_final_payload = final_payload
    except Exception as exc:
        st.session_state.messages.append({"role": "assistant", "content": f"Error: {exc}"})
        with st.chat_message("assistant"):
            st.error(f"Error: {exc}")

# Render persisted suggested actions, pending tool calls, and approvals on every rerun.
render_suggested_actions(st.session_state.get("last_final_payload", {}))
