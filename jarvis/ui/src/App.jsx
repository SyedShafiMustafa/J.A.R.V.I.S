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
  const [provider, setProvider] = useState(null);
  const [healthOk, setHealthOk] = useState(false);
  const [connected, setConnected] = useState(false);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [activityEvents, setActivityEvents] = useState([]);
  const [pendingAction, setPendingAction] = useState(null);
  const [clarification, setClarification] = useState(null);
  const [visual, setVisual] = useState(null);
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
      if (nextState.provider) setProvider(nextState.provider);
      return;
    }

    if (type === 'provider') {
      const next = data.provider || null;
      setProvider(next);
      if (next) pushActivity('BRAIN ACTIVE', providerLabel(next));
      return;
    }

    if (type === 'status') {
      setStatus(data.status);
      // "idle" is internal machinery — surface STANDBY, never a bare IDLE.
      pushActivity(data.status === 'idle' ? 'STANDBY' : data.status.toUpperCase());
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

    // Milestone 3 — visual computer agent state: target, app/window,
    // confidence, current tool and verification state. No screenshots
    // or raw screen contents ever travel in these frames.
    if (type === 'visual_update') {
      const summary = data.target || data.tool || 'visual agent';
      const detail = [data.stage, data.confidence != null ? `conf ${data.confidence}` : null, data.verified === true ? 'VERIFIED' : data.verified === false ? 'UNVERIFIED' : null].filter(Boolean).join(' // ');
      setVisual({ summary, detail, stage: data.stage, verified: data.verified, time: Date.now() });
      pushActivity(`VISUAL ${data.stage || 'UPDATE'}`, `${summary}${detail ? ` — ${detail}` : ''}`);
      return;
    }

    if (type === 'confirmation_required') {
      // Gated tool actions carry action_id and get Approve/Reject
      // buttons. Organize previews arrive WITHOUT action_id (they are
      // answered yes/no by voice or text) — those render as an info
      // banner instead of dead buttons that 400 on confirm.
      if (data.action_id) {
        setPendingAction({ action_id: data.action_id, tool: data.tool });
        pushActivity('CONFIRMATION REQUIRED', data.tool || 'action');
      } else {
        setPendingAction({ action_id: null, tool: data.tool, question: data.question || '' });
        pushActivity('CONFIRMATION REQUIRED', data.question || data.tool || 'action');
      }
      return;
    }

    // Scheduler + organizer lifecycle: previously dropped silently.
    if (type === 'scheduled') {
      const label = data.kind === 'cancel'
        ? `REMINDERS CANCELLED (${data.cancelled ?? 0})`
        : `REMINDER SET (${data.action || data.kind || 'reminder'})`;
      pushActivity(label, '');
      return;
    }

    if (type === 'organize_preview') {
      pushActivity('ORGANIZE PREVIEW', `${data.moves ?? 0} moves in ${data.directory || ''}`);
      return;
    }

    if (type === 'organize_done') {
      pushActivity('ORGANIZE DONE', data.message || '');
      return;
    }

    if (type === 'organize_cancelled') {
      pushActivity('ORGANIZE CANCELLED', '');
      return;
    }

    if (type === 'confirmation_resolved') {
      setPendingAction(null);
      pushActivity('CONFIRMATION RESOLVED', data.approved ? 'APPROVED' : 'REJECTED');
      return;
    }

    // JARVIS found more than one plausible match (similar contact or group
    // names) and needs the user to pick the intended one.
    if (type === 'clarification_required') {
      const options = Array.isArray(data.options) ? data.options.filter(Boolean) : [];
      setClarification({ question: data.question || 'Which one did you mean?', options });
      pushActivity('CLARIFICATION NEEDED', options.join(' / ') || data.question || '');
      return;
    }

    if (type === 'clarification_resolved') {
      setClarification(null);
      pushActivity(
        'CLARIFICATION RESOLVED',
        data.cancelled ? 'CANCELLED' : data.choice || '',
      );
      return;
    }

    if (type === 'emergency_stop') {
      setPendingAction(null);
      setClarification(null);
      pushActivity('EMERGENCY STOP');
      return;
    }

    if (type === 'error') {
      setErrorMessage(data.message || 'Unknown error');
      setStatus('error');
      // Stale question banners must not survive a failure: answering
      // them afterwards acts on dead state.
      setPendingAction(null);
      setClarification(null);
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

  const submitCommand = async (text) => {
    if (!text || !text.trim()) return;
    setSending(true);
    try {
      await api.sendCommand(text.trim());
    } catch (err) {
      setErrorMessage(err.message || 'Command failed');
    } finally {
      setSending(false);
    }
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text) return;
    setInput('');
    await submitCommand(text);
  };

  const handleClarify = async (choice) => {
    setClarification(null);
    pushActivity('CLARIFICATION', String(choice).toUpperCase());
    await submitCommand(choice);
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

  const handleEmergencyStop = async () => {
    setVoiceStopped(true);
    setPendingAction(null);
    try {
      await api.emergencyStop();
      pushActivity('EMERGENCY STOP');
    } catch (err) {
      setErrorMessage(err.message || 'Emergency stop failed');
    }
  };

  const handleConfirm = async (approve) => {
    if (!pendingAction) return;
    try {
      await api.confirmAction(pendingAction.action_id, approve);
      setPendingAction(null);
      pushActivity('CONFIRMATION', approve ? 'APPROVED' : 'REJECTED');
    } catch (err) {
      setErrorMessage(err.message || 'Confirmation failed');
    }
  };

  const lastToolEvent = toolEvents[0];
  const activeStatus = {
    OFFLINE: { label: 'OFFLINE', detail: 'Backend connection unavailable' },
    IDLE: { label: 'STANDBY', detail: 'Voice control disabled' },
    WAKE_LISTENING: { label: 'LISTENING FOR "HEY JARVIS"', detail: 'Waiting for wake word...' },
    WAKE_DETECTED: { label: 'WAKE DETECTED', detail: 'Preparing command capture...' },
    CAPTURING: { label: 'LISTENING', detail: 'Speak now' },
    TRANSCRIBING: { label: 'TRANSCRIBING', detail: 'Converting speech to text...' },
    THINKING: { label: 'THINKING', detail: 'Processing request...' },
    EXECUTING: { label: 'EXECUTING', detail: 'Executing task...' },
    VISUAL_OBSERVE: { label: 'VISUAL OBSERVE', detail: 'Capturing screen state...' },
    VISUAL_LOCATE: { label: 'LOCATING TARGET', detail: 'Finding UI target...' },
    VISUAL_ACT: { label: 'ACTION', detail: 'Interacting with UI...' },
    VISUAL_VERIFY: { label: 'VERIFYING', detail: 'Verifying visual result...' },
    VISUAL_RECOVER: { label: 'RECOVERING', detail: 'Retrying with new strategy...' },
    WINDOW_SWITCH: { label: 'WINDOW SWITCH', detail: 'Switching applications...' },
    APP_FOCUS: { label: 'APP FOCUS', detail: 'Managing application window...' },
    EXTRACTING: { label: 'EXTRACTING', detail: 'Reading application content...' },
    TRANSFERRING: { label: 'TRANSFERRING', detail: 'Running cross-app workflow...' },
    SETTINGS_READ: { label: 'SETTINGS READ', detail: 'Reading system setting...' },
    SETTINGS_CHANGE: { label: 'SETTINGS CHANGE', detail: 'Changing system setting...' },
    VERIFYING: { label: 'VERIFYING', detail: 'Verifying setting change...' },
    FALLBACK: { label: 'FALLBACK', detail: 'Retrying via UI automation...' },
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
            <div className="intel-row"><span>MEMORY CORE</span><strong>{connected ? 'READY' : 'N/A'}</strong></div>
            <div className="intel-row"><span>TOOL SYSTEM</span><strong>{lastToolEvent ? 'ACTIVE' : 'IDLE'}</strong></div>
            <div className="brain-indicator" data-fallback={provider?.fallback_used === true}>
              <span className="brain-label">ACTIVE BRAIN</span>
              <strong>{provider ? providerLabel(provider) : 'CONFIGURING...'}</strong>
              <small>
                PRIMARY: {(provider?.primary || 'OLLAMA').toUpperCase()}
                {provider?.fallback ? ` // FALLBACK: ${provider.fallback.toUpperCase()}` : ''}
              </small>
            </div>
            <div className="intel-task">{lastToolEvent ? `${lastToolEvent.kind.toUpperCase()} // ${lastToolEvent.tool}` : 'NO ACTIVE TASK'}</div>
            <div className="intel-row"><span>VISUAL AGENT</span><strong>{visual ? visual.summary.toUpperCase().slice(0, 28) : 'IDLE'}</strong></div>
            {visual && <div className="intel-task">{`${visual.stage || 'UPDATE'}${visual.verified === true ? ' // VERIFIED' : visual.verified === false ? ' // UNVERIFIED' : ''}`}</div>}
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
            <button className="stop-button" onClick={handleEmergencyStop} aria-label="Emergency stop">
              <span className="talk-symbol">✕</span>
              <span>EMERGENCY STOP</span>
            </button>
            <div className="dock-hint">CENTRAL J // PRIMARY VOICE CONTROL — CLICK THE J TO TOGGLE LISTENING</div>
            {clarification && (
              <div className="clarify-banner">
                <span>{clarification.question}</span>
                <div className="clarify-options">
                  {clarification.options.map((option) => (
                    <button
                      key={option}
                      className="clarify-chip"
                      onClick={() => handleClarify(option)}
                    >
                      {option}
                    </button>
                  ))}
                  <button className="clarify-cancel" onClick={() => handleClarify('cancel')}>
                    CANCEL
                  </button>
                </div>
              </div>
            )}
            {pendingAction && pendingAction.action_id && (
              <div className="confirm-banner">
                <span>CONFIRM ACTION // {String(pendingAction.tool || '').toUpperCase()}</span>
                <button className="confirm-approve" onClick={() => handleConfirm(true)}>CONFIRM</button>
                <button className="confirm-reject" onClick={() => handleConfirm(false)}>CANCEL</button>
              </div>
            )}
            {pendingAction && !pendingAction.action_id && (
              <div className="confirm-banner">
                <span>{pendingAction.question || 'Reply yes or no to continue.'}</span>
              </div>
            )}
            {errorMessage && <div className="error-banner"><span>{errorMessage}</span><button className="error-dismiss" onClick={() => setErrorMessage('')}>DISMISS</button></div>}
          </section>
        </main>
      </div>
    </>
  );
}

function providerLabel(provider) {
  if (!provider) return 'N/A';
  const vendor = (provider.vendor || provider.active || provider.primary || '').toUpperCase();
  const model = (provider.model || '').split('/').pop().replace(/-/g, ' ').toUpperCase();
  const base = model ? `${vendor} \u00b7 ${model}` : vendor;
  return provider.fallback_used ? `${base} (FALLBACK)` : base;
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
  .brain-indicator { margin-top: 16px; border: 1px solid var(--line); padding: 11px 13px; }
  .brain-indicator .brain-label { display: block; color: var(--muted); font-size: 9px; letter-spacing: 1.6px; }
  .brain-indicator strong { display: block; color: var(--cyan); font-size: 11px; margin-top: 6px; letter-spacing: .5px; }
  .brain-indicator small { display: block; color: var(--muted); font-size: 8px; margin-top: 5px; letter-spacing: 1px; }
  .brain-indicator[data-fallback='true'] { border-color: #7a6a2f; }
  .brain-indicator[data-fallback='true'] strong { color: #e8c76a; }
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
  .stop-button { border: 1px solid var(--danger); color: var(--danger); background: rgba(255,90,90,.06); cursor: pointer; min-width: 220px; padding: 0 22px; min-height: 44px; display: flex; align-items: center; justify-content: center; gap: 12px; letter-spacing: 1px; font-weight: 600; }
  .stop-button:hover { background: rgba(255,90,90,.16); }
  .confirm-banner { grid-column: 1 / -1; border: 1px solid var(--cyan); color: var(--cyan); padding: 10px 12px; font-size: 11px; display: flex; align-items: center; gap: 12px; }
  .confirm-banner span { flex: 1; }
  .confirm-approve, .confirm-reject { border: 1px solid var(--cyan); background: transparent; color: var(--cyan); cursor: pointer; padding: 5px 12px; letter-spacing: 1px; }
  .confirm-reject { border-color: var(--danger); color: var(--danger); }
  /* "which one did you mean?" — pick the intended contact/group */
  .clarify-banner { grid-column: 1 / -1; border: 1px solid var(--cyan); background: rgba(84,229,232,.05); color: var(--cyan); padding: 10px 12px; font-size: 11px; display: flex; flex-direction: column; gap: 8px; }
  .clarify-options { display: flex; flex-wrap: wrap; gap: 8px; }
  .clarify-chip { border: 1px solid var(--cyan); background: transparent; color: var(--cyan); cursor: pointer; padding: 4px 10px; letter-spacing: 1px; }
  .clarify-chip:hover { background: rgba(84,229,232,.15); }
  .clarify-cancel { border: 1px solid var(--line); background: transparent; color: var(--muted); cursor: pointer; padding: 4px 10px; letter-spacing: 1px; }
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
