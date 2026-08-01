import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Send, Database, Zap, RefreshCw, CheckCircle, AlertCircle, Loader, Sparkles } from 'lucide-react';

const API_URL = 'http://localhost:8000';

// ---------------------------------------------------------------------------
// SSE stream reader — reads /chat/stream and calls onEvent for each payload
// ---------------------------------------------------------------------------
async function readSSEStream(response, onEvent, signal) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done || signal?.aborted) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n\n');
      buffer = lines.pop(); // keep incomplete chunk
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith('data: ')) {
          try {
            onEvent(JSON.parse(trimmed.slice(6)));
          } catch (_) { /* skip malformed */ }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// Step indicator component — renders a single streaming step
// ---------------------------------------------------------------------------
function StreamStep({ step, isComplete = false }) {
  const icons = {
    thinking:   isComplete ? <CheckCircle size={14} /> : <Loader size={14} className="spin" />,
    query:      <Database size={14} />,
    executing:  isComplete ? <CheckCircle size={14} /> : <Loader size={14} className="spin" />,
    healing:    <RefreshCw size={14} />,
    error:      <AlertCircle size={14} />,
    cache_hit:  <Sparkles size={14} />,
    result:     <CheckCircle size={14} />,
    escalated:  <AlertCircle size={14} />,
  };
  const labels = {
    thinking:   `Generating query (attempt ${step.attempt ?? 1})…`,
    query:      'Query generated',
    executing:  'Executing query…',
    healing:    `Healing — attempt ${step.attempt ?? 2}…`,
    error:      `DB error (attempt ${step.attempt ?? 1})`,
    cache_hit:  '⚡ Semantic cache hit — skipping LLM',
    result:     'Done',
    escalated:  'Escalated',
  };

  return (
    <div className={`stream-step stream-step--${step.event}`}>
      {icons[step.event] ?? null}
      <span>{labels[step.event] ?? step.event}</span>
      {step.event === 'query' && step.query && (
        <code className="stream-step__query">{step.query}</code>
      )}
      {step.event === 'error' && step.error && (
        <code className="stream-step__error">{step.error}</code>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main App
// ---------------------------------------------------------------------------
function App() {
  const [messages, setMessages]     = useState([]);
  const [inputValue, setInputValue] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [dbStatus, setDbStatus]     = useState({ connected: false, type: null });
  const messagesEndRef              = useRef(null);
  const abortRef                    = useRef(null);
  const streamIdRef                 = useRef(0);   // incremented on every send; prevents stale finally
  const initialized                 = useRef(false);

  const [sessionId] = useState(() =>
    sessionStorage.getItem('qs_session') ||
    (() => { const id = Math.random().toString(36).slice(2); sessionStorage.setItem('qs_session', id); return id; })()
  );

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  useEffect(() => {
    if (!initialized.current) {
      initialized.current = true;
      addBotMessage(
        "Hello! I'm QueryStream, your AI-powered database assistant. What type of database would you like to connect to?",
        ['PostgreSQL', 'MySQL', 'MongoDB']
      );
    }
  }, []);

  const addBotMessage = (text, options = null, data = null, query = null, steps = null, cacheHit = false) => {
    setMessages(prev => [...prev, { sender: 'bot', text, options, data, query, steps, cacheHit }]);
  };

  const addUserMessage = (text) => {
    setMessages(prev => [...prev, { sender: 'user', text }]);
  };

  // Append a live "streaming" message placeholder and return its updater
  const addStreamingMessage = () => {
    const id = Date.now();
    setMessages(prev => [...prev, { id, sender: 'bot', streaming: true, steps: [], text: '', data: null, query: null, cacheHit: false }]);
    return id;
  };

  const updateStreamingMessage = (id, updater) => {
    setMessages(prev => prev.map(m => m.id === id ? { ...m, ...updater(m) } : m));
  };

  const finaliseStreamingMessage = (id) => {
    setMessages(prev => prev.map(m => m.id === id ? { ...m, streaming: false } : m));
  };

  // ── Core send handler ────────────────────────────────────────────────────
  const handleSendMessage = useCallback(async (text, isOption = false) => {
    // Text-input sends are blocked while streaming; option button clicks are always allowed
    // (they abort the current stream and start a new one).
    if (isStreaming && !isOption) return;

    // Abort any in-flight stream and stamp a new stream ID.
    // The old stream's finally/catch checks this ID before touching shared state,
    // so it silently exits instead of overwriting the new stream's isStreaming flag.
    abortRef.current?.abort();
    abortRef.current  = new AbortController();
    const myStreamId  = ++streamIdRef.current;

    setIsStreaming(true);
    const msgId = addStreamingMessage();

    try {
      const res = await fetch(`${API_URL}/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, message: text, is_option: isOption }),
        signal: abortRef.current.signal,
      });

      if (!res.ok) throw new Error(`Server error ${res.status}`);

      await readSSEStream(res, (payload) => {
        if (myStreamId !== streamIdRef.current) return; // stale — new send already started
        const { event } = payload;

        if (event === 'message') {
          updateStreamingMessage(msgId, () => ({
            text:    payload.reply ?? '',
            options: payload.options ?? null,
            steps:   [],
          }));
          if (payload.state === 'CONNECTED') {
            setDbStatus({ connected: true, type: payload.db_type });
          }
          return;
        }

        if (event === 'done') return;

        if (event === 'result') {
          updateStreamingMessage(msgId, m => ({
            text:  payload.reply ?? '',
            data:  payload.data ?? null,
            query: payload.query ?? null,
            steps: [...(m.steps ?? []), { event, ...payload }],
          }));
          return;
        }

        if (event === 'escalated' || event === 'stream_error') {
          updateStreamingMessage(msgId, m => ({
            text:  payload.reply ?? payload.error ?? 'An error occurred.',
            steps: [...(m.steps ?? []), { event, ...payload }],
          }));
          return;
        }

        if (event === 'cache_hit') {
          updateStreamingMessage(msgId, m => ({
            cacheHit: true,
            steps: [...(m.steps ?? []), { event, ...payload }],
          }));
          return;
        }

        updateStreamingMessage(msgId, m => ({
          steps: [...(m.steps ?? []), { event, ...payload }],
        }));

      }, abortRef.current.signal);

    } catch (err) {
      if (myStreamId !== streamIdRef.current) return; // stale — ignore
      if (err.name !== 'AbortError') {
        updateStreamingMessage(msgId, () => ({
          text: 'Sorry, I encountered a connection error. Is the backend running?',
        }));
        console.error(err);
      }
    } finally {
      // Only reset shared state if this is still the active stream.
      if (myStreamId === streamIdRef.current) {
        finaliseStreamingMessage(msgId);
        setIsStreaming(false);
      }
    }
  }, [isStreaming, sessionId]);

  // Option button clicks always go through — they abort the current stream
  // (if any) and start a new send immediately without being blocked by isStreaming.
  const handleOptionClick = (opt) => {
    addUserMessage(opt);
    handleSendMessage(opt, true);
  };
  const handleSubmit = (e) => {
    e.preventDefault();
    if (!inputValue.trim() || isStreaming) return;
    addUserMessage(inputValue);
    handleSendMessage(inputValue, false);
    setInputValue('');
  };

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="app-container">
      {/* Sidebar */}
      <div className="sidebar">
        <h1><Database size={24} color="#3b82f6" />QueryStream</h1>

        <div className="connection-status">
          <h3 style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '0.5rem', textTransform: 'uppercase', letterSpacing: '1px' }}>
            Connection
          </h3>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span className={`status-indicator ${dbStatus.connected ? 'connected' : 'disconnected'}`} />
            <span style={{ color: 'var(--text-primary)', fontSize: '0.9rem' }}>
              {dbStatus.connected ? `Connected · ${dbStatus.type}` : 'Disconnected'}
            </span>
          </div>
        </div>

        <div className="sidebar-info">
          <div className="sidebar-badge"><Zap size={12} />SSE Streaming</div>
          <div className="sidebar-badge"><Sparkles size={12} />Semantic Cache</div>
        </div>

        <div style={{ marginTop: 'auto', fontSize: '0.75rem', color: 'var(--text-secondary)', textAlign: 'center' }}>
          Powered by Google Gemini
        </div>
      </div>

      {/* Chat */}
      <div className="chat-container">
        <div className="chat-messages">
          {messages.map((msg, idx) => (
            <div key={msg.id ?? idx} className={`message ${msg.sender}`}>

              {/* Bot: stream steps */}
              {msg.sender === 'bot' && msg.steps && msg.steps.length > 0 && (
                <div className="stream-steps">
                  {msg.steps.map((s, i) => (
                    <StreamStep
                      key={i}
                      step={s}
                      isComplete={!msg.streaming || i < msg.steps.length - 1}
                    />
                  ))}
                </div>
              )}

              {/* Cache badge */}
              {msg.cacheHit && (
                <div className="cache-badge"><Sparkles size={12} /> Served from semantic cache</div>
              )}

              {/* Message text */}
              {msg.text && <div>{msg.text}</div>}

              {/* Streaming pulse */}
              {msg.streaming && !msg.text && (
                <div className="typing-indicator">
                  <div className="typing-dot" /><div className="typing-dot" /><div className="typing-dot" />
                </div>
              )}

              {/* Generated query accordion */}
              {msg.query && (
                <details className="query-accordion">
                  <summary>View Generated Query</summary>
                  <div className="sql-query">{msg.query}</div>
                </details>
              )}

              {/* Results table */}
              {msg.data && msg.data.length > 0 && (
                <div className="data-table-container">
                  <table className="data-table">
                    <thead>
                      <tr>{Object.keys(msg.data[0]).map(k => <th key={k}>{k}</th>)}</tr>
                    </thead>
                    <tbody>
                      {msg.data.map((row, i) => (
                        <tr key={i}>
                          {Object.values(row).map((v, j) => (
                            <td key={j}>{v !== null ? v.toString() : 'NULL'}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {/* Option buttons */}
              {msg.options && (
                <div className="options-container">
                  {msg.options.map((opt, i) => (
                    <button key={i} className="option-btn" onClick={() => handleOptionClick(opt)}>{opt}</button>
                  ))}
                </div>
              )}
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>

        <div className="chat-input-container">
          <form className="input-wrapper" onSubmit={handleSubmit}>
            <input
              id="chat-input"
              type="text"
              className="chat-input"
              placeholder="Ask anything about your database…"
              value={inputValue}
              onChange={e => setInputValue(e.target.value)}
              disabled={isStreaming}
              autoComplete="off"
            />
            <button
              id="send-btn"
              type="submit"
              className="send-btn"
              disabled={!inputValue.trim() || isStreaming}
            >
              {isStreaming ? <Loader size={18} className="spin" /> : <Send size={18} />}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}

export default App;
