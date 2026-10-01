"""FastAPI server: chat with the co-pilot and stream everything to the browser.

POST /api/chat only queues a message; the reply, tool calls, job progress and
state updates all arrive on the session's single SSE stream (GET /api/events).
That way agent turns triggered by the user and by finished background jobs
render the same way, and the user can chat while a fit is running.
"""
import asyncio
import io
import json
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

import modeling
from agent import root_agent
from workspace import WORKSPACES, Workspace, get_workspace

log = logging.getLogger("meridian_app")
APP_NAME, USER_ID = "meridian_copilot", "local"
STATIC = Path(__file__).parent / "static"

sessions = InMemorySessionService()
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=sessions)
streaming = RunConfig(streaming_mode=StreamingMode.SSE)
app = FastAPI(title="Meridian Co-pilot")
_tasks = set()  # keep references to fire-and-forget tasks


def spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def workspace(session_id: str) -> Workspace:
    if session_id not in WORKSPACES:
        raise HTTPException(404, "unknown session; reload the page")
    return WORKSPACES[session_id]


async def agent_turn(ws: Workspace, text: str):
    """Run one co-pilot turn and publish its output as UI events."""
    async with ws.agent_lock:  # ADK sessions take one turn at a time
        notice = text.removeprefix("[system] ") if text.startswith("[system]") else None
        ws.publish("agent_start", notice=notice)
        message = types.Content(role="user", parts=[types.Part(text=text)])
        streamed = False  # partial chunks already sent for the current message
        try:
            async for event in runner.run_async(
                user_id=USER_ID, session_id=ws.session_id,
                new_message=message, run_config=streaming,
            ):
                for part in event.content.parts if event.content else []:
                    if part.text and not part.thought and (event.partial or not streamed):
                        ws.publish("delta", text=part.text)
                    if part.function_call and not event.partial:  # partials are fragments
                        ws.publish("tool_call", name=part.function_call.name,
                                   args=part.function_call.args)
                    if part.function_response:
                        ws.publish("tool_result", name=part.function_response.name,
                                   response=part.function_response.response)
                streamed = bool(event.partial)
        except Exception as e:
            log.exception("agent turn failed")
            ws.publish("error", message=f"Agent error: {e}")
        finally:
            ws.publish("agent_end")


# ---------------------------------------------------------------- API


class ChatIn(BaseModel):
    session_id: str
    text: str


class PriorsIn(BaseModel):
    session_id: str
    priors: dict[str, dict[str, float]]


class SessionIn(BaseModel):
    session_id: str


@app.post("/api/session")
async def new_session():
    session = await sessions.create_session(app_name=APP_NAME, user_id=USER_ID)
    ws = await asyncio.to_thread(get_workspace, session.id)  # loads the sample data
    ws.notify = lambda text: agent_turn(ws, text)
    return {"session_id": session.id}


@app.get("/api/events")
async def events(session_id: str, request: Request):
    ws = workspace(session_id)
    queue = ws.subscribe()

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'state', 'state': ws.snapshot()})}\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event, default=str)}\n\n"
        finally:
            ws.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/chat")
async def chat(body: ChatIn):
    spawn(agent_turn(workspace(body.session_id), body.text))
    return {"ok": True}


@app.post("/api/upload")
async def upload(session_id: str = Form(...), file: UploadFile = File(...)):
    ws = workspace(session_id)
    try:
        frame = pd.read_csv(io.BytesIO(await file.read()))
    except Exception as e:
        raise HTTPException(400, f"could not read CSV: {e}")
    ws.set_upload(file.filename, frame)
    spawn(agent_turn(ws, f"[system] The user uploaded {file.filename} ({len(frame)} rows, "
                         f"columns: {list(frame.columns)}). Inspect it and propose a column mapping, "
                         f"then wait for the user to confirm before calling map_csv_columns."))
    return {"ok": True}


@app.post("/api/priors")
async def set_priors(body: PriorsIn):
    try:
        workspace(body.session_id).set_priors(body.priors)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/fit")
async def fit(body: SessionIn):
    try:
        return workspace(body.session_id).start_fit()
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/stop")
async def stop(body: SessionIn):
    try:
        return workspace(body.session_id).stop()
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/runs/{run_id}/charts")
async def run_charts(run_id: int, session_id: str):
    try:
        return workspace(session_id).run(run_id).charts
    except ValueError as e:
        raise HTTPException(404, str(e))


modeling.REPORTS_DIR.mkdir(exist_ok=True)
app.mount("/reports", StaticFiles(directory=modeling.REPORTS_DIR), name="reports")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
