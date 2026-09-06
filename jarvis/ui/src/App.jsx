import { useState, useEffect, useRef, useCallback } from 'react';
import { api, connectWebSocket, disconnectWebSocket } from './api';

const STATUS_LABELS = {
  idle: 'Idle',
  listening: 'Listening',
  thinking: 'Thinking',
  executing: 'Executing',
  speaking: 'Speaking',
  error: 'Error',
};

const STATUS_CLASSES = {
  idle: 'status status-idle',
  listening: 'status status-listening',
  thinking: 'status status-thinking',
  executing: 'status status-executing',
  speaking: 'status status-speaking',
  error: 'status status-error',
};

export default function App() {
  const [status, setStatus] = useState('idle');
  const [transcript, setTranscript] = useState('');
  const [reply, setReply] = useState('');
  const [toolEvents, setToolEvents] = useState([]);
  const [errorMessage, setErrorMessage] = useState('');
  const [healthOk, setHealthOk] = useState(false);
  const [connected, setConnected] = useState(false);
  const [listening, setListening] = useState(false);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const chatEndRef = useRef(null);
  const inputRef = useRef(null);

  const pushToolEvent = useCallback((event) => {
    setToolEvents((prev) => {
      const next = [event, ...prev];
      return next.length > 50 ? next.slice(0, 50) : next;
    });
  }, []);

  const applyEvent = useCallback((data) => {
    const type = data.type;

    if (type === 'status') {
      setStatus(data.status);
      setListening(data.status === 'listening');
      if (data.status === 'error') {
        setErrorMessage('Connection or runtime error.');
      }
      return;
    }

    if (type === 'user_text') {
      setTranscript(data.text);
      return;
    }

    if (type === 'reply') {
      setReply(data.text);
      return;
    }

    if (type === 'bus_event') {
      const event = data.event;
      if (event === 'transcription_ready' || event === 'stt.ready') {
        setTranscript((meta) => data.meta?.user_text || transcript);
      }
      if (event === 'tool.started') {
        pushToolEvent({
          kind: 'started',
          tool: data.meta?.tool || 'unknown',
          time: Date.now(),
        });
      }
      if (event === 'tool.finished') {
        pushToolEvent({
          kind: 'finished',
          tool: data.meta?.tool || 'unknown',
          success: data.meta?.success,
          time: Date.now(),
        });
      }
      if (event === 'tool.failed') {
        pushToolEvent({
          kind: 'failed',
          tool: data.meta?.tool || 'unknown',
          error: data.meta?.error,
          time: Date.now(),
        });
      }
      if (event === 'session.started' || event === 'session.ended') {
        // session lifecycle observed; UI remains responsive
      }
      return;
    }

    if (type === 'error') {
      setErrorMessage(data.message || 'Unknown error');
      setStatus('error');
      return;
    }

    if (type === 'pong' || type === 'subscribed') {
      setConnected(true);
      return;
    }
  }, [transcript, pushToolEvent]);

  useEffect(() => {
    let cancelled = false;

    const ping = async () => {
      try {
        const health = await api.health();
        if (!cancelled) {
          setHealthOk(health?.ok === true);
        }
      } catch {
        if (!cancelled) {
          setHealthOk(false);
        }
      }
    };

    ping();
    const healthInterval = setInterval(ping, 8000);

    let wsReady = false;

    const connect = async () => {
      try {
        await connectWebSocket((event) => {
          if (!cancelled) {
            applyEvent(event);
          }
        });
        wsReady = true;
        if (!cancelled) {
          setConnected(true);
        }
      } catch {
        if (!cancelled) {
          setConnected(false);
        }
      }
    };

    connect();
    const ws = connectWebSocket((event) => {
      if (!cancelled) {
        applyEvent(event);
      }
    });

    return () => {
      cancelled = true;
      clearInterval(healthInterval);
      disconnectWebSocket();
    };
  }, [applyEvent]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [reply, transcript, toolEvents]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text) return;
    setInput('');
    setSending(true);
    try {
      await api.sendCommand(text);
    } catch (err) {
      setErrorMessage(err.message || 'Command failed');
    } finally {
      setSending(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const toggleListening = async () => {
    if (listening) {
      try {
        await api.stopListening();
      } catch (err) {
        setErrorMessage(err.message || 'Stop failed');
      }
    } else {
      try {
        await api.startListening();
      } catch (err) {
        setErrorMessage(err.message || 'Start failed');
      }
    }
  };

  const lastToolEvent = toolEvents[0];

  return (
    <>
      <style>{styles}</style>
      <div className="app">
        <header className="topbar">
          <div className="brand">
            <span className="logo">J</span>
            <span className="title">Jarvis</span>
          </div>
          <div className="topbar-right">
            <span className={STATUS_CLASSES[status] || STATUS_CLASSES.idle}>
              {STATUS_LABELS[status] || 'Idle'}
            </span>
            <span className="conn" data-ok={healthOk && connected}>
              {healthOk && connected ? 'Live' : 'Offline'}
            </span>
          </div>
        </header>

        <div className="panels">
          <section className="panel control-panel">
            <div className="control-row">
              <button
                className="mic-button"
                data-listening={listening}
                onClick={toggleListening}
                aria-label={listening ? 'Stop listening' : 'Start listening'}
              >
                <span className="mic-icon">
                  {listening ? '.stop' : 'mic'}
                </span>
                <span className="mic-label">
                  {listening ? 'Stop listening' : 'Start listening'}
                </span>
              </button>
              <div className="control-status">
                <span className="state-chip">{STATUS_LABELS[status]}</span>
                {lastToolEvent && (
                  <span className="tool-chip">
                    {lastToolEvent.kind} {lastToolEvent.tool}
                  </span>
                )}
              </div>
            </div>

            <div className="command-row">
              <input
                ref={inputRef}
                className="command-input"
                placeholder="Send a command..."
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={sending}
              />
              <button
                className="send-button"
                onClick={handleSend}
                disabled={!input.trim() || sending}
              >
                {sending ? '...' : 'Send'}
              </button>
            </div>

            {errorMessage && (
              <div className="error-banner">
                <span>{errorMessage}</span>
                <button className="error-dismiss" onClick={() => setErrorMessage('')}>
                  dismiss
                </button>
              </div>
            )}
          </section>

          <section className="panel transcript-panel">
            <div className="panel-header">
              <h2>Transcript</h2>
            </div>
            <div className="transcript-block">
              {transcript ? (
                <p className="user-line">You: {transcript}</p>
              ) : (
                <p className="empty-line">No transcript yet</p>
              )}
            </div>
          </section>

          <section className="panel reply-panel">
            <div className="panel-header">
              <h2>Reply</h2>
            </div>
            <div className="reply-block">
              {reply ? (
                <p className="reply-line">Jarvis: {reply}</p>
              ) : (
                <p className="empty-line">Waiting for a reply</p>
              )}
              <div className="reply-spacer" ref={chatEndRef} />
            </div>
          </section>

          <section className="panel tool-panel">
            <div className="panel-header">
              <h2>Tool activity</h2>
            </div>
            <ul className="tool-list">
              {toolEvents.length === 0 ? (
                <li className="empty-line">No tool events yet</li>
              ) : (
                toolEvents.slice(0, 50).map((event, i) => (
                  <li key={i} className={`tool-item tool-${event.kind}`}>
                    <span className="tool-time">{formatTime(event.time)}</span>
                    <span className="tool-name">{event.tool || 'tool'}</span>
                    {event.kind === 'failed' && event.error && (
                      <span className="tool-error">{event.error}</span>
                    )}
                  </li>
                ))
              )}
            </ul>
          </section>
        </div>
      </div>
    </>
  );
}

function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

const styles = `
  :root {
    --bg: #0b0d10;
    --panel: #15171c;
    --panel-2: #1b1e23;
    --border: #262a31;
    --text: #e7e9ea;
    --muted: #9aa0a6;
    --accent: #6b4bff;
    --green: #1f6f43;
    --green-soft: #b4f5cf;
    --purple: #7a4a8c;
    --purple-soft: #e8c6f5;
    --blue: #2b6bff;
    --red: #7a1f1f;
    --red-soft: #ffb4b4;
  }

  * { box-sizing: border-box; }

  html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  }

  .app {
    max-width: 900px;
    margin: 0 auto;
    padding: 24px 20px 48px;
  }

  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 22px;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .logo {
    width: 36px;
    height: 36px;
    border-radius: 10px;
    background: linear-gradient(135deg, #6b4bff, #2b6bff);
    color: #fff;
    font-weight: 700;
    font-size: 20px;
    display: grid;
    place-items: center;
    box-shadow: 0 6px 18px rgba(107, 75, 255, 0.25);
  }

  .title {
    font-size: 26px;
    letter-spacing: 0.4px;
  }

  .topbar-right {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .status {
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.7px;
  }

  .status-idle { background: #2a2d33; color: #9aa0a6; }
  .status-listening { background: var(--green); color: var(--green-soft); }
  .status-thinking { background: var(--blue); color: #d6e0ff; }
  .status-executing { background: var(--purple); color: var(--purple-soft); }
  .status-speaking { background: #7a4a2a; color: #ffd9b4; }
  .status-error { background: var(--red); color: var(--red-soft); }

  .conn {
    font-size: 12px;
    color: var(--muted);
  }

  .conn[data-ok='true'] {
    color: var(--green-soft);
  }

  .panels {
    display: grid;
    gap: 16px;
  }

  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
  }

  .panel-header h2 {
    margin: 0;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1.1px;
    color: var(--muted);
    font-weight: 600;
  }

  .control-panel {
    display: grid;
    gap: 14px;
  }

  .control-row {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }

  .mic-button {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px;
    border: none;
    border-radius: 12px;
    background: var(--panel-2);
    color: var(--text);
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s ease, transform 0.05s ease, box-shadow 0.15s ease;
    box-shadow: 0 1px 2px rgba(0,0,0,0.3);
  }

  .mic-button:hover {
    background: #23262c;
  }

  .mic-button:active {
    transform: translateY(1px);
  }

  .mic-button[data-listening='true'] {
    background: var(--red);
    color: var(--red-soft);
    box-shadow: 0 0 0 1px var(--red-soft);
  }

  .mic-button[data-listening='true']:hover {
    background: #8a2525;
  }

  .mic-icon {
    width: 22px;
    height: 22px;
    display: grid;
    place-items: center;
    font-size: 18px;
  }

  .mic-label {
    white-space: nowrap;
  }

  .control-status {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
    flex-wrap: wrap;
  }

  .state-chip {
    padding: 6px 10px;
    border-radius: 8px;
    background: var(--panel-2);
    font-size: 12px;
    color: var(--muted);
    border: 1px solid var(--border);
  }

  .tool-chip {
    padding: 6px 10px;
    border-radius: 8px;
    background: rgba(107, 75, 255, 0.15);
    color: var(--purple-soft);
    font-size: 12px;
    border: 1px solid rgba(107, 75, 255, 0.3);
  }

  .command-row {
    display: flex;
    gap: 10px;
  }

  .command-input {
    flex: 1;
    padding: 12px 14px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: var(--panel-2);
    color: var(--text);
    font-size: 14px;
    outline: none;
    transition: border-color 0.15s ease;
  }

  .command-input:focus {
    border-color: var(--accent);
  }

  .send-button {
    padding: 12px 18px;
    border: none;
    border-radius: 12px;
    background: var(--accent);
    color: #fff;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    transition: opacity 0.15s ease, transform 0.05s ease;
  }

  .send-button:hover:not(:disabled) {
    opacity: 0.9;
  }

  .send-button:active:not(:disabled) {
    transform: translateY(1px);
  }

  .send-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .error-banner {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 10px;
    background: rgba(122, 31, 31, 0.2);
    border: 1px solid var(--red-soft);
    color: var(--red-soft);
    font-size: 13px;
  }

  .error-dismiss {
    border: none;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-decoration: underline;
    opacity: 0.8;
    font-size: 12px;
  }

  .transcript-panel,
  .reply-panel {
    display: grid;
    gap: 6px;
  }

  .transcript-block,
  .reply-block {
    background: var(--panel-2);
    border-radius: 10px;
    padding: 12px 14px;
    min-height: 60px;
  }

  .user-line,
  .reply-line {
    margin: 0;
  }

  .user-line {
    color: var(--green-soft);
  }

  .reply-line {
    color: var(--text);
  }

  .empty-line {
    margin: 0;
    color: var(--muted);
  }

  .reply-spacer {
    height: 6px;
  }

  .tool-panel {
    display: grid;
    gap: 6px;
  }

  .tool-list {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 160px;
    overflow: auto;
  }

  .tool-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border-radius: 8px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    font-size: 13px;
  }

  .tool-time {
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    min-width: 70px;
  }

  .tool-name {
    color: var(--text);
    font-weight: 500;
  }

  .tool-started {
    border-left: 3px solid var(--blue);
  }

  .tool-finished {
    border-left: 3px solid var(--green);
  }

  .tool-failed {
    border-left: 3px solid var(--red);
  }

  .tool-error {
    margin-left: auto;
    color: var(--red-soft);
    font-size: 12px;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
`;
