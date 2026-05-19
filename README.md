# AI Homelab Assistant (ai-stack)

FastAPI backend plus a chat-centric Streamlit frontend for local model interactions and approvals.

Run the API locally (after installing dependencies):

```bash
python -m pip install -r requirements.txt
uvicorn backend.api.main:app --reload --host 0.0.0.0 --port 8000
```

Run the Streamlit UI against the backend:

```bash
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

If the backend is not on `http://127.0.0.1:8000`, set `BACKEND_URL` before launching Streamlit.
