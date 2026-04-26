import os
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from mcp_server.orchestrator import run_orchestrator
from tools.final_formatter import format_html_output

app = FastAPI(title="MCP AI Web Scraper API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")

executor = ThreadPoolExecutor(max_workers=2)


class ScrapeRequest(BaseModel):
    url: str
    query: str


@app.get("/")
async def root():
    return FileResponse("frontend/index.html")


@app.post("/scrape")
async def scrape(request: ScrapeRequest):
    loop = asyncio.get_event_loop()
    raw_data = await loop.run_in_executor(executor, run_orchestrator, request.url, request.query)
    html_output = await loop.run_in_executor(executor, format_html_output, raw_data, request.query)
    return {"status": "success", "raw_data": raw_data, "html_output": html_output}


@app.get("/health")
async def health():
    return {"status": "ok"}
