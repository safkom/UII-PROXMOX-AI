from __future__ import annotations

import json
import os
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
        "include_logs": True,
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

    assistant_box = st.chat_message("assistant")
    text_slot = assistant_box.empty()
    status_slot = assistant_box.empty()

    accumulated = ""
    final_payload: dict[str, Any] | None = None
    buffer = ""

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        buffer += raw_line + "\n"
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
    return final_payload


if "messages" not in st.session_state:
    st.session_state.messages = []
if "approvals" not in st.session_state:
    st.session_state.approvals = []
if "models" not in st.session_state:
    st.session_state.models = []
if "selected_model" not in st.session_state:
    st.session_state.selected_model = ""
if "backend_url" not in st.session_state:
    st.session_state.backend_url = DEFAULT_BACKEND_URL

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
    st.session_state.selected_model = st.selectbox(
        "Model",
        options=[""] + st.session_state.models,
        index=0 if st.session_state.selected_model not in st.session_state.models else st.session_state.models.index(st.session_state.selected_model) + 1,
        format_func=lambda value: value or "default model",
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

user_query = st.chat_input("Ask about the homelab, logs, or available actions...")
if user_query:
    st.session_state.messages.append({"role": "user", "content": user_query})
    with st.chat_message("user"):
        st.markdown(user_query)

    try:
        final_payload = stream_chat(user_query, st.session_state.selected_model)
        assistant_summary = str(final_payload.get("summary", ""))
        st.session_state.messages.append({"role": "assistant", "content": assistant_summary})

        suggested_actions = final_payload.get("suggested_actions", [])
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

                    col1, col2, col3 = st.columns(3)
                    if command:
                        with col1:
                            if st.button("Create approval", key=f"create-{user_query}-{action_title}-{command}"):
                                try:
                                    created = backend_post(
                                        "/approvals",
                                        {
                                            "action": action_title,
                                            "command": command,
                                            "target": target,
                                            "risk": risk,
                                            "source_query": user_query,
                                            "requested_by": "streamlit",
                                        },
                                    ).json()
                                    st.session_state.approvals.insert(0, created)
                                    st.success(f"Created approval {created['id']}")
                                except Exception as exc:
                                    st.error(f"Failed to create approval: {exc}")
                        with col2:
                            if st.button("Create + approve", key=f"approve-{user_query}-{action_title}-{command}"):
                                try:
                                    created = backend_post(
                                        "/approvals",
                                        {
                                            "action": action_title,
                                            "command": command,
                                            "target": target,
                                            "risk": risk,
                                            "source_query": user_query,
                                            "requested_by": "streamlit",
                                        },
                                    ).json()
                                    approved = backend_patch(
                                        f"/approvals/{created['id']}",
                                        {"decision": "approved", "reviewer": "streamlit", "note": "approved from Streamlit chat"},
                                    ).json()
                                    st.session_state.approvals.insert(0, approved)
                                    st.success(f"Approved {approved['id']}")
                                except Exception as exc:
                                    st.error(f"Failed to approve approval: {exc}")

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
                if approval.get("status") == "approved" and approval.get("command"):
                    with cols[2]:
                        if st.button("Execute", key=f"execute-card-{approval['id']}"):
                            try:
                                result = backend_execute(str(approval["id"]))
                                st.code(json.dumps(result, indent=2), language="json")
                            except Exception as exc:
                                st.error(f"Execution failed: {exc}")
    except Exception as exc:
        st.session_state.messages.append({"role": "assistant", "content": f"Error: {exc}"})
        with st.chat_message("assistant"):
            st.error(f"Error: {exc}")
