"""Minimal FastAPI app. The eval task asks for request logging to be added."""
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/orders")
def create_order(order: dict):
    return {"id": 1, "total": order.get("total", 0)}
