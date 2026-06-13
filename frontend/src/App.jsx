import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { Send, Database, Server, Cloud, CheckCircle, AlertCircle } from 'lucide-react';

const API_URL = 'http://localhost:8000';

function App() {
  const [messages, setMessages] = useState([]);
  const [inputValue, setInputValue] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  const [dbStatus, setDbStatus] = useState({ connected: false, type: null });
  const messagesEndRef = useRef(null);
  
  // Create a unique session ID for this user
  const [sessionId] = useState(() => Math.random().toString(36).substring(7));

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const initialized = useRef(false);

  useEffect(() => {
    // Initial greeting
    if (!initialized.current) {
      initialized.current = true;
      addBotMessage(
        "Hello! I am PELLM, your AI Database Assistant. I can help you query your database using natural language. To get started, what type of database would you like to connect to?",
        ['PostgreSQL', 'MySQL', 'MongoDB']
      );
    }
  }, []);

  const addBotMessage = (text, options = null, data = null, query = null) => {
    setMessages(prev => [...prev, { sender: 'bot', text, options, data, query }]);
  };

  const addUserMessage = (text) => {
    setMessages(prev => [...prev, { sender: 'user', text }]);
  };

  const handleOptionClick = (option) => {
    addUserMessage(option);
    handleSendMessage(option, true);
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!inputValue.trim()) return;
    
    addUserMessage(inputValue);
    handleSendMessage(inputValue, false);
    setInputValue('');
  };

  const handleSendMessage = async (text, isOption = false) => {
    setIsTyping(true);
    try {
      const response = await axios.post(`${API_URL}/chat`, {
        session_id: sessionId,
        message: text,
        is_option: isOption
      });
      
      const { reply, options, state, data, query } = response.data;
      
      setIsTyping(false);
      addBotMessage(reply, options, data, query);
      
      if (state === 'CONNECTED') {
        setDbStatus({ connected: true, type: response.data.db_type });
      }
    } catch (error) {
      setIsTyping(false);
      addBotMessage("Sorry, I encountered an error connecting to the server. Is the backend running?");
      console.error(error);
    }
  };

  return (
    <div className="app-container">
      <div className="sidebar">
        <h1>
          <Database size={24} color="#3b82f6" />
          PELLM
        </h1>
        
        <div className="connection-status">
          <h3 style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginBottom: '0.5rem', textTransform: 'uppercase', letterSpacing: '1px' }}>
            Connection Status
          </h3>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '1rem' }}>
            <span className={`status-indicator ${dbStatus.connected ? 'connected' : 'disconnected'}`}></span>
            {dbStatus.connected ? (
              <span style={{ color: 'var(--text-primary)' }}>Connected to {dbStatus.type}</span>
            ) : (
              <span style={{ color: 'var(--text-secondary)' }}>Disconnected</span>
            )}
          </div>
        </div>

        <div style={{ marginTop: 'auto', fontSize: '0.8rem', color: 'var(--text-secondary)', textAlign: 'center' }}>
          Powered by Google GenAI
        </div>
      </div>

      <div className="chat-container">
        <div className="chat-messages">
          {messages.map((msg, index) => (
            <div key={index} className={`message ${msg.sender}`}>
              <div>{msg.text}</div>
              
              {msg.query && (
                <details className="query-accordion">
                  <summary>View Generated Query</summary>
                  <div className="sql-query">
                    {msg.query}
                  </div>
                </details>
              )}
              
              {msg.data && msg.data.length > 0 && (
                <div className="data-table-container">
                  <table className="data-table">
                    <thead>
                      <tr>
                        {Object.keys(msg.data[0]).map((key) => (
                          <th key={key}>{key}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {msg.data.map((row, i) => (
                        <tr key={i}>
                          {Object.values(row).map((val, j) => (
                            <td key={j}>{val !== null ? val.toString() : 'NULL'}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {msg.options && (
                <div className="options-container">
                  {msg.options.map((opt, i) => (
                    <button 
                      key={i} 
                      className="option-btn"
                      onClick={() => handleOptionClick(opt)}
                    >
                      {opt}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
          {isTyping && (
            <div className="message bot">
              <div className="typing-indicator">
                <div className="typing-dot"></div>
                <div className="typing-dot"></div>
                <div className="typing-dot"></div>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        <div className="chat-input-container">
          <form className="input-wrapper" onSubmit={handleSubmit}>
            <input
              type="text"
              className="chat-input"
              placeholder="Type your message or query..."
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              disabled={isTyping}
            />
            <button type="submit" className="send-btn" disabled={!inputValue.trim() || isTyping}>
              <Send size={18} />
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}

export default App;
