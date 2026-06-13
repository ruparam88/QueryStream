import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from agent import ChatManager

app = FastAPI()

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    session_id: str
    message: str
    is_option: Optional[bool] = False

class ChatResponse(BaseModel):
    reply: str
    options: Optional[List[str]] = None
    state: str
    db_type: Optional[str] = None
    data: Optional[List[Dict[str, Any]]] = None
    query: Optional[str] = None

# Store chat managers in memory for sessions
sessions: Dict[str, ChatManager] = {}

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    if request.session_id not in sessions:
        sessions[request.session_id] = ChatManager()
    
    manager = sessions[request.session_id]
    
    try:
        response = manager.process_message(request.message)
        return ChatResponse(**response)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
