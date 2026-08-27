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
- [🐳 Running with Docker (Recommended)](#-running-with-docker-recommended)
  - [1. Configure Environment](#1-configure-environment)
  - [2. Start QueryStream Stack](#2-start-querystream-stack)
  - [3. Spin Up a Database Container](#3-spin-up-a-database-container)
  - [4. Connect Database to QueryStream Network](#4-connect-database-to-querystream-network)
  - [5. Connect & Query via Web Interface](#5-connect--query-via-web-interface)
- [🛠️ Local Manual Setup](#️-local-manual-setup)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Environment Variables](#environment-variables)
- [💡 Usage](#-usage)
- [🗺️ Roadmap & Contributing](#-roadmap--contributing)
- [📄 License & Acknowledgments](#-license--acknowledgments)

---

## 🚀 About the Project

**QueryStream** translates natural language directly into executable database queries. By integrating seamlessly with PostgreSQL, MySQL, and MongoDB, it serves as a zero-knowledge query agent. 

Unlike basic LLM wrappers that blindly guess schema structures, QueryStream utilizes **Dynamic Schema RAG**, a **self-healing execution loop**, and **semantic caching** to deliver accurate, secure, and blazing-fast data insights without writing a single line of SQL.

---

## 📸 Outputs / Screenshots

Here is a glimpse of QueryStream in action:

<div align="center">
  <img src="Outputs/screenshot-1.png" width="45%" alt="QueryStream Screenshot 1" />
  <img src="Outputs/screenshot-2.png" width="45%" alt="QueryStream Screenshot 2" />
  <br />
  <img src="Outputs/screenshot-3.png" width="45%" alt="QueryStream Screenshot 3" />
  <img src="Outputs/screenshot-4.png" width="45%" alt="QueryStream Screenshot 4" />
</div>

---

## ✨ Key Features

- 🧠 **Dynamic Schema RAG**: Automatically introspects your live database schema (DDL and Foreign Keys) and injects it into the LLM context to prevent hallucinations and achieve high execution accuracy.
- 🔄 **Self-Healing LLM Loop**: Uses LangGraph state machines and AST semantic validation to automatically retry and repair queries if execution fails.
- 🛡️ **Strict Security Guardrails**: Enforces read-only execution. Destructive operations (INSERT, DROP, DELETE) are instantly quarantined and rejected.
- ⚡ **Real-Time Streaming**: Employs a stateless, horizontally scalable asynchronous event stream using WebSockets/SSE to provide instant feedback ("thinking", "executing").
- 💾 **Redis Semantic Caching**: Caches semantically similar queries using vector embeddings to bypass the LLM entirely, reducing latency to under 450ms and cutting token costs by 40%.

---

## 🐳 Running with Docker (Recommended)

Run the entire QueryStream stack (Frontend, Backend, and Redis) with a single command using Docker Compose.

### 1. Configure Environment

Create a `.env` file in the root directory and add your Google Gemini API key:

```env
GEMINI_API_KEY="your-google-gemini-api-key"
```

*(On Windows PowerShell, you can also set it directly for the session: `$env:GEMINI_API_KEY="your-google-gemini-api-key"`)*

### 2. Start QueryStream Stack

Build and start all services:

```bash
docker compose up --build
```

- **Frontend**: [http://localhost:3000](http://localhost:3000)
- **Backend API & Swagger Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Redis Cache**: `localhost:6379`

---

### 3. Spin Up a Database Container

If you don't already have a database running, you can create one easily in Docker:

#### Option A: PostgreSQL Container
```bash
docker run -d --name my-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=pass123 \
  -e POSTGRES_DB=employees \
  -p 5432:5432 \
  postgres:16-alpine
```

#### Option B: MySQL Container
```bash
docker run -d --name my-mysql \
  -e MYSQL_ROOT_PASSWORD=pass123 \
  -e MYSQL_DATABASE=employees \
  -p 3306:3306 \
  mysql:8.0
```

#### Option C: MongoDB Container
```bash
docker run -d --name my-mongo \
  -e MONGO_INITDB_ROOT_USERNAME=admin \
  -e MONGO_INITDB_ROOT_PASSWORD=pass123 \
  -p 27017:27017 \
  mongo:7.0
```

---

### 4. Connect Database to QueryStream Network

To allow the QueryStream backend container to communicate directly with your database container by name, connect your database to QueryStream's Docker network:

1. **Check the network name:**
   ```bash
   docker network ls
   ```
   *(Usually named `querystream_querystream-net`)*

2. **Attach your container to the network:**
   ```bash
   # For PostgreSQL:
   docker network connect querystream_querystream-net my-postgres

   # For MySQL:
   docker network connect querystream_querystream-net my-mysql

   # For MongoDB:
   docker network connect querystream_querystream-net my-mongo
   ```

---

### 5. Connect & Query via Web Interface

1. Open your browser and navigate to **[http://localhost:3000](http://localhost:3000)**.
2. Select your database type and enter the connection string using your container name:

| Database | Connection String Format |
| :--- | :--- |
| **PostgreSQL** | `postgresql://postgres:pass123@my-postgres:5432/employees` |
| **MySQL** | `mysql://root:pass123@my-mysql:3306/employees` |
| **MongoDB** | `mongodb://admin:pass123@my-mongo:27017` |

*(Note: If connecting to a database running natively on your host machine outside Docker, use `host.docker.internal` instead of the container name).*

3. Once connected, start asking queries in plain English!

---

## 🛠️ Local Manual Setup

If you prefer to run the services manually without Docker:

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

## 💡 Usage (Manual Mode)

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
- [x] Provide Docker Compose support for a 1-click full-stack deployment.

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
