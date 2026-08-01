<div align="center">
  
# 🌊 QueryStream

**An enterprise-grade, agentic Text-to-SQL system that lets you chat with your database.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-18.x-61DAFB.svg?logo=react)](https://reactjs.org/)

</div>

---

## 📑 Table of Contents

- [About the Project](#-about-the-project)
- [Key Features](#-key-features)
- [Getting Started](#-getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Environment Variables](#environment-variables)
- [Usage](#-usage)
- [Roadmap & Contributing](#-roadmap--contributing)
- [License & Acknowledgments](#-license--acknowledgments)

---

## 🚀 About the Project

**QueryStream** translates natural language directly into executable database queries. By integrating seamlessly with PostgreSQL, MySQL, and MongoDB, it serves as a zero-knowledge query agent. 

Unlike basic LLM wrappers that blindly guess schema structures, QueryStream utilizes **Dynamic Schema RAG**, a **self-healing execution loop**, and **semantic caching** to deliver accurate, secure, and blazing-fast data insights without writing a single line of SQL.

---

## ✨ Key Features

- 🧠 **Dynamic Schema RAG**: Automatically introspects your live database schema (DDL and Foreign Keys) and injects it into the LLM context to prevent hallucinations and achieve high execution accuracy.
- 🔄 **Self-Healing LLM Loop**: Uses LangGraph state machines and AST semantic validation to automatically retry and repair queries if execution fails.
- 🛡️ **Strict Security Guardrails**: Enforces read-only execution. Destructive operations (INSERT, DROP, DELETE) are instantly quarantined and rejected.
- ⚡ **Real-Time Streaming**: Employs a stateless, horizontally scalable asynchronous event stream using WebSockets/SSE to provide instant feedback ("thinking", "executing").
- 💾 **Redis Semantic Caching**: Caches semantically similar queries using vector embeddings to bypass the LLM entirely, reducing latency to under 450ms and cutting token costs by 40%.

---

## 🛠️ Getting Started

### Prerequisites

Ensure you have the following installed on your system:
- **Node.js** (v18+)
- **Python** (v3.10+)
- **Redis Server** (running locally or remotely)
- **Google Gemini API Key**

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/ruparam88/QueryStream.git
   cd QueryStream
   ```

2. **Backend Setup:**
   ```bash
   cd backend
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Frontend Setup:**
   ```bash
   cd ../frontend
   npm install
   ```

### Environment Variables

In the `backend` directory, create a `.env` file based on the provided `.env.example`:

```env
# Google Gemini API Config
GEMINI_API_KEY="your-google-gemini-api-key"
GEMINI_MODEL="gemini-2.5-flash"
GEMINI_EMBED_MODEL="gemini-embedding-2"

# Redis Config
REDIS_URL="redis://localhost:6379"

# Security & Limits
MAX_QUERY_ATTEMPTS=3
SEM_CACHE_THRESHOLD=0.95
```

---

## 💡 Usage

1. **Start the Redis Server** (if running locally):
   ```bash
   redis-server
   ```

2. **Start the Backend Server:**
   ```bash
   cd backend
   uvicorn main:app --reload
   ```

3. **Start the Frontend Application:**
   ```bash
   cd frontend
   npm run dev
   ```

4. **Connect & Query:**
   - Open `http://localhost:5173` in your browser.
   - Follow the chatbot prompts to connect your local or cloud database (e.g., `postgresql://user:password@localhost:5432/dbname`).
   - Ask a question in plain English!

> **Example Query:**
> *"Perform a left join between the users and orders table to show all users who made a purchase in the last 30 days."*

---

## 🗺️ Roadmap & Contributing

### Future Plans
- [ ] Add support for Snowflake and BigQuery.
- [ ] Incorporate comprehensive observability (LangSmith/Phoenix).
- [ ] Provide Docker Compose support for a 1-click full-stack deployment.

### Contributing
Contributions are what make the open source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📄 License & Acknowledgments

Distributed under the **MIT License**. See `LICENSE` for more information.

- Powered by [Google Gemini](https://deepmind.google/technologies/gemini/)
- State machine orchestration via [LangGraph](https://python.langchain.com/docs/langgraph)
- Scalable session management and caching with [Redis](https://redis.io/)
