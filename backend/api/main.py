from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .routes import router
from .security import require_api_auth

app = FastAPI(title="AI Homelab Assistant - API")
# Apply the optional API-key check to every API route. It is a no-op unless
# API_AUTH_TOKEN is configured. The /ui static mount below stays open so the
# operator can always load the page and enter their token.
app.include_router(router, dependencies=[Depends(require_api_auth)])

# Mount a simple static frontend at /ui when the `frontend` folder exists
frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
if frontend_dir.exists() and frontend_dir.is_dir():
	app.mount("/ui", StaticFiles(directory=str(frontend_dir), html=True), name="ui")
