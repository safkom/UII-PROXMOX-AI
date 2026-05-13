from fastapi import FastAPI
from .routes import router

app = FastAPI(title="AI Homelab Assistant - API")
app.include_router(router)
