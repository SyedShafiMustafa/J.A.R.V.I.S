import { useState, useEffect } from 'react';
import { api } from './api';

function StatusBadge({ status }) {
  const cls = {
    idle: 'badge badge-idle',
    listening: 'badge badge-listening',
    speaking: 'badge badge-speaking',
    working: 'badge badge-working',
    error: 'badge badge-error',
  }[status ?? 'idle'] ?? 'badge badge-idle';

  return <span className={cls}>{status ?? 'idle'}</span>;
}

export default function App() {
  const [status, setStatus] = useState('idle');
  const [lastReply, setLastReply] = useState('');
  const [lastError, setLastError] = useState('');
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const poll = async () => {
      try {
        const health = await api.health();
        if (health?.ok) {
          setReady(true);
        }
      } catch {
        // backend not ready yet
      }
    };

    const timer = setInterval(poll, 800);
    poll();
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const handler = () => {
      // In a real integration this would come from the backend via SSE/websocket.
      // For the skeleton, we just show the placeholder states here.
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, []);

  return (
    <>
      <style>{styles}</style>
      <div className="app">
        <header className="header">
          <h1>Jarvis</h1>
          <StatusBadge status={ready ? status : 'idle'} />
        </header>

        <main className="main">
          <section className="panel">
            <h2>Status</h2>
            <p className="muted">Backend ready: {ready ? 'yes' : 'no'}</p>
            <p className="muted">UI port: 5173</p>
            <p className="muted">API proxy: /api -&gt; localhost:8000</p>
          </section>

          <section className="panel">
            <h2>Last reply</h2>
            <p className="reply">{lastReply || '—'}</p>
          </section>

          {lastError && (
            <section className="panel panel-error">
              <h2>Error</h2>
              <p>{lastError}</p>
            </section>
          )}
        </main>
      </div>
    </>
  );
}

const styles = `
  * { box-sizing: border-box; }

  body {
    margin: 0;
    background: #0b0d10;
    color: #e7e9ea;
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  }

  .app {
    max-width: 880px;
    margin: 0 auto;
    padding: 40px 20px;
  }

  .header {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 28px;
  }

  h1 {
    margin: 0;
    font-size: 28px;
    letter-spacing: 0.5px;
  }

  .badge {
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }

  .badge-idle { background: #2a2d33; color: #9aa0a6; }
  .badge-listening { background: #1f6f43; color: #b4f5cf; }
  .badge-speaking { background: #7a4a8c; color: #e8c6f5; }
  .badge-working { background: #6b4bff; color: #d6d0ff; }
  .badge-error { background: #7a1f1f; color: #ffb4b4; }

  .main {
    display: grid;
    gap: 16px;
  }

  .panel {
    background: #15171c;
    border: 1px solid #262a31;
    border-radius: 12px;
    padding: 16px 18px;
  }

  .panel-error {
    border-color: #5a2222;
    background: #1a1010;
  }

  h2 {
    margin: 0 0 10px 0;
    font-size: 14px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #8b9198;
  }

  .muted {
    margin: 0;
    color: #9aa0a6;
  }

  .reply {
    margin: 0;
    color: #e7e9ea;
  }
`;
