import { useState, useEffect, useRef, useCallback } from 'react';
import { api, connectWebSocket, disconnectWebSocket } from './api';

export default function App() {
  const [status, setStatus] = useState('offline');
  const [phase, setPhase] = useState('IDLE');
  const [voiceStopped, setVoiceStopped] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [reply, setReply] = useState('');
  const [toolEvents, setToolEvents] = useState([]);
  const [errorMessage, setErrorMessage] = useState('');
  const [healthOk, setHealthOk] = useState(false);
  const [connected, setConnected] = useState(false);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [activityEvents, setActivityEvents] = useState([]);
  const [currentTime, setCurrentTime] = useState(new Date());
  const chatEndRef = useRef(null);
  const inputRef = useRef(null);

  const pushToolEvent = useCallback((event) => {
    setToolEvents((prev) => {
      const next = [event, ...prev];
      return next.length > 50 ? next.slice(0, 50) : next;
    });
  }, []);

  const pushActivity = useCallback((label, detail = '') => {
    setActivityEvents((prev) => {
      const next = [{ label, detail, time: Date.now() }, ...prev];
      return next.slice(0, 30);
    });
  }, []);

  const applyEvent = useCallback((data) => {
    const type = data.type;

    if (type === 'state') {
      const nextState = data.state || {};
      if (nextState.status) {
        setStatus(nextState.status);
      }
      if (nextState.phase) setPhase(nextState.phase);
      if (nextState.transcript) setTranscript(nextState.transcript);
      if (nextState.reply) setReply(nextState.reply);
      if (nextState.error_message) setErrorMessage(nextState.error_message);
      return;
    }

    if (type === 'status') {
      setStatus(data.status);
      pushActivity(data.status.toUpperCase());
      if (data.status === 'error') {
        setErrorMessage('Connection or runtime error.');
      }
      return;
    }

    if (type === 'user_text') {
      setTranscript(data.text);
      pushActivity('TRANSCRIPT READY', data.text);
      return;
    }

    if (type === 'reply') {
      setReply(data.text);
      pushActivity('RESPONSE', data.text);
      return;
    }

    if (type === 'bus_event') {
      const event = data.event;
      if (event === 'wake.listening') {
        const stopped = data.meta?.stopped === true;
        if (!stopped) {
          pushActivity('WAKE LISTENER READY');
        } else {
          pushActivity('WAKE LISTENER STOPPED');
        }
      }
      if (event === 'audio.start') {
        pushActivity('MICROPHONE ACTIVE');
      }
      if (event === 'audio.stop') {
        pushActivity('MICROPHONE IDLE');
      }
      if (event === 'transcription_ready' || event === 'stt.ready') {
        setTranscript((current) => data.meta?.user_text || current);
        pushActivity('STT COMPLETE');
      }
      if (event === 'tool.started') {
        pushActivity('TOOL STARTED', data.meta?.tool || 'unknown');
        pushToolEvent({
          kind: 'started',
          tool: data.meta?.tool || 'unknown',
          time: Date.now(),
        });
      }
      if (event === 'tool.finished') {
        pushActivity('TOOL COMPLETE', data.meta?.tool || 'unknown');
        pushToolEvent({
          kind: 'finished',
          tool: data.meta?.tool || 'unknown',
          success: data.meta?.success,
          time: Date.now(),
        });
      }
      if (event === 'tool.failed') {
        pushActivity('TOOL FAILED', data.meta?.error || data.meta?.tool || 'unknown');
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
      pushActivity('CONNECTION ERROR', data.message || 'Unknown error');
      return;
    }

    if (type === 'pong' || type === 'subscribed') {
      setConnected(true);
      return;
    }
  }, [transcript, pushActivity, pushToolEvent]);

  const visiblePhase = !healthOk || !connected ? 'OFFLINE' : phase;
  // While the voice loop is armed and nothing else is happening, the resting
  // state IS wake listening. Do not surface a bare "IDLE" as the primary
  // voice state; a deliberate stop via the central J still shows IDLE.
  const displayPhase = visiblePhase === 'IDLE' && !voiceStopped ? 'WAKE_LISTENING' : visiblePhase;
  const listeningPhases = new Set(['WAKE_LISTENING', 'WAKE_DETECTED', 'CAPTURING', 'TRANSCRIBING']);
  const listening = listeningPhases.has(displayPhase);

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
    const clockInterval = setInterval(() => setCurrentTime(new Date()), 1000);

    const connect = async () => {
      try {
        await connectWebSocket((event) => {
          if (!cancelled) {
            applyEvent(event);
          }
        });
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

    return () => {
      cancelled = true;
      clearInterval(healthInterval);
      clearInterval(clockInterval);
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
      setVoiceStopped(true);
      try {
        await api.stopListening();
      } catch (err) {
        setErrorMessage(err.message || 'Stop failed');
      }
    } else {
      setVoiceStopped(false);
      try {
        await api.startListening();
      } catch (err) {
        setErrorMessage(err.message || 'Start failed');
      }
    }
  };

  const lastToolEvent = toolEvents[0];
  const activeStatus = {
    OFFLINE: { label: 'OFFLINE', detail: 'Backend connection unavailable' },
    IDLE: { label: 'IDLE', detail: 'Awaiting activity' },
    WAKE_LISTENING: { label: 'LISTENING FOR "HEY JARVIS"', detail: 'Waiting for wake word...' },
    WAKE_DETECTED: { label: 'WAKE DETECTED', detail: 'Preparing command capture...' },
    CAPTURING: { label: 'LISTENING', detail: 'Speak now' },
    TRANSCRIBING: { label: 'TRANSCRIBING', detail: 'Converting speech to text...' },
    THINKING: { label: 'THINKING', detail: 'Processing request...' },
    EXECUTING: { label: 'EXECUTING', detail: 'Executing task...' },
    SPEAKING: { label: 'SPEAKING', detail: 'Responding...' },
    STOPPING: { label: 'STOPPING', detail: 'Releasing voice resources...' },
    ERROR: { label: 'ERROR', detail: 'Attention required' },
  }[displayPhase] || { label: 'OFFLINE', detail: 'Backend connection unavailable' };
  const activity = activityEvents.length ? activityEvents : [{ label: 'SYSTEM READY', detail: 'Awaiting activity', time: Date.now() }];

  return (
    <>
      <style>{styles}</style>
      <div className="app">
        <header className="system-bar">
          <div className="brand">
            <span className="logo">J</span>
            <div>
              <div className="title">J.A.R.V.I.S.</div>
              <div className="subtitle">PERSONAL AI COMMAND SYSTEM</div>
            </div>
          </div>
          <div className="system-readout">
            <span className="online-indicator" data-ok={healthOk && connected}>
              <i /> {healthOk && connected ? 'SYSTEM ONLINE' : 'BACKEND OFFLINE'}
            </span>
            <span>LOCAL NODE</span>
            <span className="clock">{currentTime.toLocaleTimeString([], { hour12: false })}</span>
          </div>
        </header>

        <main className="hud-grid">
          <aside className="side-panel telemetry-panel">
            <div className="panel-label">SYSTEM TELEMETRY <span>01</span></div>
            <div className="telemetry-list">
              <div className="telemetry-row"><span>CPU LOAD</span><strong>N/A</strong></div>
              <div className="telemetry-row"><span>MEMORY</span><strong>N/A</strong></div>
              <div className="telemetry-row"><span>NETWORK</span><strong className={healthOk ? 'good' : ''}>{healthOk ? 'CONNECTED' : 'N/A'}</strong></div>
              <div className="telemetry-row"><span>MICROPHONE</span><strong className={listening ? 'good' : ''}>{listening ? 'ACTIVE' : 'READY'}</strong></div>
              <div className="telemetry-row"><span>WAKE DETECTOR</span><strong className={displayPhase === 'WAKE_LISTENING' ? 'good' : ''}>{displayPhase === 'WAKE_LISTENING' ? 'ACTIVE' : 'IDLE'}</strong></div>
            </div>
            <div className="telemetry-divider" />
            <div className="panel-label">VOICE SUBSYSTEM <span>02</span></div>
            <div className="voice-readout">
              <div className="voice-dot" data-active={listening} />
              <div><strong>{activeStatus.label}</strong><small>{activeStatus.detail}</small></div>
            </div>
            <div className="metric-line"><span>BACKEND</span><b>{connected ? 'CONNECTED' : 'DISCONNECTED'}</b></div>
            <div className="metric-line"><span>SESSION</span><b>{connected ? 'READY' : 'N/A'}</b></div>
          </aside>

          <section className={`core-panel core-${visiblePhase}`}>
            <div className="core-kicker">JARVIS / CORE PROCESSOR</div>
            <button
              type="button"
              className="core-visual"
              data-listening={listening}
              data-error={visiblePhase === 'ERROR'}
              onClick={toggleListening}
              aria-label={`${activeStatus.label} — click to ${listening ? 'stop' : 'start'} listening`}
              title={listening ? 'Stop listening' : 'Start listening'}
            >
              <div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="orbit orbit-three" />
              <div className="core-ticks">{Array.from({ length: 18 }, (_, i) => <i key={i} style={{ transform: `rotate(${i * 20}deg)` }} />)}</div>
              <div className="core-glow"><span>J</span></div>
              <div className="core-phase">{activeStatus.label}</div>
            </button>
            <div className="core-status">
              <span className="status-mark">●</span>
              <strong>{activeStatus.label}</strong>
              <small>{activeStatus.detail}</small>
            </div>
            <div className="core-caption">VOICE INTERFACE // {healthOk && connected ? 'OPERATIONAL' : 'STANDBY'}</div>
          </section>

          <aside className="side-panel intelligence-panel">
            <div className="panel-label">INTELLIGENCE <span>03</span></div>
            <div className="intel-row"><span>BRAIN / LLM</span><strong>{healthOk ? 'READY' : 'N/A'}</strong></div>
            <div className="intel-row"><span>MEMORY CORE</span><strong>{connected ? 'READY' : 'N/A'}</strong></div>
            <div className="intel-row"><span>TOOL SYSTEM</span><strong>{lastToolEvent ? 'ACTIVE' : 'IDLE'}</strong></div>
            <div className="intel-task">{lastToolEvent ? `${lastToolEvent.kind.toUpperCase()} // ${lastToolEvent.tool}` : 'NO ACTIVE TASK'}</div>
            <div className="panel-label activity-title">ACTIVITY STREAM <span>04</span></div>
            <div className="activity-stream">
              {activity.map((event, i) => <div className="activity-item" key={`${event.time}-${i}`}><time>{formatTime(event.time)}</time><span>{event.label}</span><small>{event.detail}</small></div>)}
            </div>
          </aside>

          <section className="conversation-panel">
            <div className="hud-heading"><span>LIVE TRANSCRIPT</span><em>VOICE CHANNEL</em></div>
            <div className="conversation-columns">
              <div className="conversation-block"><span className="channel-tag">USER INPUT</span><p>{transcript || 'Awaiting voice input...'}</p></div>
              <div className="conversation-block"><span className="channel-tag">J.A.R.V.I.S.</span><p>{reply || 'No response signal.'}</p></div>
            </div>
            <div ref={chatEndRef} />
          </section>

          <section className="command-dock">
            <button className="talk-button" data-listening={listening} onClick={toggleListening} aria-label={listening ? 'Stop listening' : 'Talk to Jarvis'}>
              <span className="talk-symbol">{listening ? '■' : '◉'}</span>
              <span>{listening ? 'STOP LISTENING' : 'START LISTENING'}</span>
            </button>
            <div className="command-input-wrap">
              <span className="input-prefix">&gt;</span>
              <input ref={inputRef} className="command-input" placeholder="Enter a text command..." value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={handleKeyDown} disabled={sending} />
              <button className="send-button" onClick={handleSend} disabled={!input.trim() || sending}>{sending ? '...' : 'TRANSMIT'}</button>
            </div>
            <div className="dock-hint">CENTRAL J // PRIMARY VOICE CONTROL — CLICK THE J TO TOGGLE LISTENING</div>
            {errorMessage && <div className="error-banner"><span>{errorMessage}</span><button className="error-dismiss" onClick={() => setErrorMessage('')}>DISMISS</button></div>}
          </section>
        </main>
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
  :root { --bg: #050a0d; --panel: rgba(8, 20, 25, .86); --line: #173840; --cyan: #54e5e8; --blue: #61a8ff; --text: #d8f4f3; --muted: #6b9298; --danger: #ff7181; }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-width: 320px; background: var(--bg); color: var(--text); font-family: 'IBM Plex Mono', Consolas, monospace; }
  body { background-image: linear-gradient(rgba(84,229,232,.025) 1px, transparent 1px), linear-gradient(90deg, rgba(84,229,232,.025) 1px, transparent 1px); background-size: 34px 34px; }
  button, input { font: inherit; }
  button:focus-visible, input:focus-visible { outline: 2px solid var(--cyan); outline-offset: 3px; }
  .app { min-height: 100vh; max-width: 1500px; margin: auto; padding: 22px 28px 30px; }
  .system-bar { border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; align-items: center; padding-bottom: 18px; }
  .brand, .system-readout, .online-indicator, .voice-readout, .metric-line, .intel-row { display: flex; align-items: center; }
  .brand { gap: 14px; }
  .logo { width: 42px; height: 42px; display: grid; place-items: center; border: 1px solid var(--cyan); color: var(--cyan); font: 700 22px Orbitron, sans-serif; box-shadow: 0 0 18px rgba(84,229,232,.35), inset 0 0 12px rgba(84,229,232,.15); }
  .title { color: var(--cyan); font: 600 20px 'Arial Narrow', Arial, sans-serif; letter-spacing: 3px; }
  .subtitle, .system-readout, .panel-label, .core-kicker, .core-caption, .hud-heading, .channel-tag { color: var(--muted); font-size: 10px; letter-spacing: 1.8px; }
  .subtitle { margin-top: 5px; letter-spacing: 1px; }
  .system-readout { gap: 24px; font-size: 10px; letter-spacing: 1px; }
  .online-indicator { gap: 7px; color: var(--muted); }
  .online-indicator[data-ok='true'] { color: var(--cyan); }
  .online-indicator i, .status-mark { width: 7px; height: 7px; display: inline-block; border-radius: 50%; background: var(--danger); box-shadow: 0 0 8px var(--danger); }
  .online-indicator[data-ok='true'] i { background: var(--cyan); box-shadow: 0 0 8px var(--cyan); }
  .clock { color: var(--cyan); font-variant-numeric: tabular-nums; }
  .hud-grid { display: grid; grid-template-columns: minmax(190px, 1fr) minmax(360px, 1.8fr) minmax(190px, 1fr); grid-template-areas: 'telemetry core intelligence' 'conversation conversation conversation' 'dock dock dock'; gap: 18px; padding-top: 22px; }
  .side-panel, .core-panel, .conversation-panel, .command-dock { background: var(--panel); border: 1px solid var(--line); position: relative; }
  .side-panel { min-height: 440px; padding: 18px; }
  .side-panel::before, .core-panel::before, .conversation-panel::before, .command-dock::before { content: ''; position: absolute; inset: 7px; border: 1px solid rgba(84,229,232,.08); pointer-events: none; }
  .telemetry-panel { grid-area: telemetry; }
  .intelligence-panel { grid-area: intelligence; }
  .panel-label { display: flex; justify-content: space-between; color: var(--cyan); border-bottom: 1px solid var(--line); padding-bottom: 12px; }
  .panel-label span { color: var(--muted); }
  .telemetry-list { margin-top: 24px; }
  .telemetry-row, .intel-row { justify-content: space-between; border-bottom: 1px solid rgba(23,56,64,.7); padding: 12px 0; font-size: 10px; }
  .telemetry-row span, .intel-row span { color: var(--muted); }
  .telemetry-row strong, .intel-row strong { color: #a0c2c5; font-size: 10px; font-weight: 500; }
  .telemetry-row strong.good, .intel-row strong { color: var(--cyan); }
  .telemetry-divider { height: 54px; border-bottom: 1px solid var(--line); margin-bottom: 18px; }
  .voice-readout { gap: 12px; padding: 20px 0 12px; }
  .voice-dot { width: 13px; height: 13px; border: 1px solid var(--muted); border-radius: 50%; }
  .voice-dot[data-active='true'] { border-color: var(--cyan); background: var(--cyan); box-shadow: 0 0 13px var(--cyan); }
  .voice-readout strong, .voice-readout small { display: block; }
  .voice-readout strong { color: var(--text); font-size: 11px; }
  .voice-readout small { color: var(--muted); font-size: 10px; margin-top: 5px; }
  .metric-line { justify-content: space-between; color: var(--muted); font-size: 10px; padding-top: 16px; }
  .metric-line b { color: var(--cyan); font-size: 9px; }
  .core-panel { grid-area: core; min-height: 440px; display: flex; flex-direction: column; align-items: center; justify-content: center; overflow: hidden; }
  .core-kicker { position: absolute; top: 21px; left: 24px; color: var(--cyan); }
  .core-visual { width: min(310px, 72vw); aspect-ratio: 1; position: relative; display: grid; place-items: center; margin: 18px 0 10px; }
  .orbit { position: absolute; border: 1px solid rgba(84,229,232,.3); border-radius: 50%; }
  .orbit-one { inset: 9%; border-right-color: transparent; border-left-color: var(--cyan); animation: spin 12s linear infinite; }
  .orbit-two { inset: 20%; border-top-color: transparent; border-bottom-color: var(--blue); animation: spin-reverse 8s linear infinite; }
  .orbit-three { inset: 32%; border-style: dashed; border-color: rgba(84,229,232,.2); animation: spin 18s linear infinite; }
  .core-glow { width: 28%; aspect-ratio: 1; display: grid; place-items: center; border: 1px solid var(--cyan); border-radius: 50%; color: var(--cyan); background: radial-gradient(circle, rgba(84,229,232,.35), rgba(8,25,29,.8) 65%); box-shadow: 0 0 22px rgba(84,229,232,.75), 0 0 80px rgba(84,229,232,.2); animation: pulse 2.7s ease-in-out infinite; z-index: 2; }
  .core-glow span { font: 700 35px 'Arial Narrow', Arial, sans-serif; text-shadow: 0 0 15px var(--cyan); }
  .core-ticks { position: absolute; inset: 3%; border-radius: 50%; }
  .core-ticks i { position: absolute; left: 50%; top: 0; width: 1px; height: 8px; background: var(--cyan); transform-origin: 50% 150px; opacity: .7; }
  .core-status { text-align: center; z-index: 2; }
  .status-mark { background: var(--cyan); box-shadow: 0 0 8px var(--cyan); vertical-align: middle; margin-right: 9px; }
  .core-status strong, .core-status small { display: inline-block; }
  .core-status strong { color: var(--cyan); font: 600 14px 'Arial Narrow', Arial, sans-serif; letter-spacing: 1px; }
  .core-status small { display: block; color: var(--muted); font-size: 10px; margin-top: 9px; letter-spacing: 1px; }
  .core-caption { margin-top: 28px; }
  .core-error .core-glow { border-color: var(--danger); box-shadow: 0 0 22px rgba(255,113,129,.65); color: var(--danger); }
  .core-error .status-mark { background: var(--danger); box-shadow: 0 0 8px var(--danger); }
  .core-error .core-status strong { color: var(--danger); }
  .activity-title { margin-top: 31px; }
  .intel-task { margin-top: 18px; min-height: 50px; border: 1px solid var(--line); padding: 13px; color: var(--cyan); font-size: 10px; line-height: 1.6; }
  .activity-stream { margin-top: 12px; max-height: 245px; overflow: auto; }
  .activity-item { display: grid; grid-template-columns: 58px 1fr; gap: 5px 9px; border-bottom: 1px solid rgba(23,56,64,.6); padding: 9px 0; font-size: 9px; }
  .activity-item time { grid-row: span 2; color: var(--muted); }
  .activity-item span { color: var(--cyan); }
  .activity-item small { overflow: hidden; color: var(--muted); white-space: nowrap; text-overflow: ellipsis; }
  .conversation-panel { grid-area: conversation; padding: 18px 22px; }
  .hud-heading { display: flex; justify-content: space-between; color: var(--cyan); }
  .hud-heading em { color: var(--muted); font-style: normal; }
  .conversation-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; margin-top: 15px; }
  .conversation-block { min-height: 85px; border-left: 2px solid var(--line); padding: 4px 16px; }
  .channel-tag { color: var(--muted); font-size: 9px; }
  .conversation-block p { margin: 12px 0 0; color: var(--text); font-size: 13px; line-height: 1.6; }
  .command-dock { grid-area: dock; display: grid; grid-template-columns: auto 1fr; gap: 18px; padding: 20px; }
  .talk-button, .send-button { border: 1px solid var(--cyan); color: var(--cyan); background: rgba(84,229,232,.06); cursor: pointer; letter-spacing: 1px; font-weight: 600; }
  .talk-button { min-width: 220px; padding: 0 22px; display: flex; align-items: center; justify-content: center; gap: 12px; }
  .talk-button:hover, .send-button:hover:not(:disabled) { background: rgba(84,229,232,.16); }
  .talk-button[data-listening='true'] { border-color: var(--danger); color: var(--danger); }
  .talk-symbol { font-size: 18px; }
  .command-input-wrap { min-width: 0; display: flex; align-items: center; border: 1px solid var(--line); }
  .input-prefix { color: var(--cyan); padding-left: 15px; }
  .command-input { flex: 1; min-width: 0; border: 0; outline: 0; padding: 15px; background: transparent; color: var(--text); font-size: 12px; }
  .command-input::placeholder { color: var(--muted); }
  .send-button { align-self: stretch; padding: 0 20px; border-width: 0 0 0 1px; }
  .send-button:disabled { color: var(--muted); cursor: not-allowed; opacity: .55; }
  .error-banner { grid-column: 1 / -1; border: 1px solid var(--danger); color: var(--danger); padding: 10px 12px; font-size: 11px; display: flex; justify-content: space-between; }
  .error-dismiss { border: 0; background: transparent; color: inherit; cursor: pointer; }
  /* central J = primary voice control */
  button.core-visual { background: none; border: 0; padding: 0; cursor: pointer; }
  button.core-visual:focus-visible { outline: 2px solid var(--cyan); outline-offset: 8px; border-radius: 50%; }
  .core-visual:hover .core-glow { box-shadow: 0 0 30px rgba(84,229,232,.9), 0 0 90px rgba(84,229,232,.3); }
  .core-visual[data-error='true'] .core-glow { border-color: var(--danger); }
  .core-visual[data-error='true'] .core-glow span { color: var(--danger); text-shadow: 0 0 15px var(--danger); }
  .core-phase { position: absolute; bottom: 4%; left: 50%; transform: translateX(-50%); border: 1px solid var(--line); background: rgba(8, 20, 25, .9); color: var(--cyan); font-size: 9px; letter-spacing: 1.5px; padding: 5px 11px; white-space: nowrap; z-index: 3; }
  .core-visual[data-listening='true'] .core-phase { border-color: var(--cyan); box-shadow: 0 0 12px rgba(84,229,232,.35); }
  .core-visual[data-error='true'] .core-phase { color: var(--danger); border-color: var(--danger); }
  /* bottom voice control demoted to fallback */
  .talk-button { border-color: var(--line); color: var(--muted); }
  .talk-button:hover { background: rgba(84,229,232,.06); }
  .talk-button[data-listening='true'] { border-color: var(--danger); color: var(--danger); }
  .dock-hint { grid-column: 1 / -1; color: var(--muted); font-size: 9px; letter-spacing: 1.5px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes spin-reverse { to { transform: rotate(-360deg); } }
  @keyframes pulse { 50% { transform: scale(1.08); opacity: .75; } }
  @media (prefers-reduced-motion: reduce) { .orbit, .core-glow { animation: none; } }
  @media (max-width: 900px) {
    .app { padding: 16px; }
    .hud-grid { grid-template-columns: 1fr 1fr; grid-template-areas: 'core core' 'telemetry intelligence' 'conversation conversation' 'dock dock'; }
    .core-panel { min-height: 380px; }
  }
  @media (max-width: 620px) {
    .system-readout > span:not(.online-indicator), .subtitle { display: none; }
    .hud-grid { display: flex; flex-direction: column; }
    .core-panel { order: -1; min-height: 350px; }
    .side-panel { min-height: auto; }
    .conversation-columns, .command-dock { grid-template-columns: 1fr; }
    .talk-button { min-height: 54px; }
    .send-button { min-height: 44px; border-width: 1px 0 0; }
  }
`;
