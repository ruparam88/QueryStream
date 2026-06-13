# PELLM - AI Database Query Assistant

This is a full-stack project built with React (Vite) and Python (FastAPI). It allows users to connect to their databases (PostgreSQL, MySQL, MongoDB), either locally or in the cloud, and query them using natural language.

## Features
- **Dynamic Database Connections**: Connects to Postgres, MySQL, and Mongo.
- **Natural Language to SQL/Mongo**: Uses Grok (`grok-beta`) to dynamically convert natural language to database queries.
- **Modern Premium UI**: Built with React, featuring a glassmorphism sidebar, gradients, micro-animations, and a responsive chat interface.
- **Stateful Conversation**: The backend remembers the connection state, first asking for DB Type, then Hosting, then Connection URI.

## Prerequisites
- Node.js (v18+)
- Python 3.9+
- An xAI (Grok) API Key

## Setup Instructions

### 1. Backend Setup
1. Open a terminal and navigate to the `backend` folder:
   ```cmd
   cd backend
   ```
2. Create a virtual environment and activate it (optional but recommended):
   ```cmd
   python -m venv venv
   venv\Scripts\activate
   ```
3. Install the required packages:
   ```cmd
   pip install -r requirements.txt
   ```
4. Set your xAI (Grok) API key as an environment variable (or hardcode it in `agent.py` for testing):
   ```cmd
   set XAI_API_KEY="your-api-key-here"
   ```
5. Run the FastAPI server:
   ```cmd
   uvicorn main:app --reload
   ```

### 2. Frontend Setup
1. Open a new terminal and navigate to the `frontend` folder:
   ```cmd
   cd frontend
   ```
2. Install the dependencies:
   ```cmd
   npm install
   ```
3. Start the Vite development server:
   ```cmd
   npm run dev
   ```

## Usage
1. Open your browser to the URL provided by Vite (usually `http://localhost:5173`).
2. The PELLM chatbot will greet you and ask what database you want to connect to.
3. Provide the connection URI. 
4. Ask your database questions in plain English!
