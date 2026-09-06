const API_BASE = '/api';
const WS_BASE = 'ws';

let ws = null;
let wsResolve = null;

export function createWsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}`;
}

export function connectWebSocket(onEvent) {
  if (ws) {
    ws.close();
  }

  const url = createWsUrl();
  ws = new WebSocket(url);

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'subscribe' }));
    if (wsResolve) {
      wsResolve();
      wsResolve = null;
    }
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      onEvent(data);
    } catch {
      // ignore malformed frames
    }
  };

  ws.onclose = () => {
    ws = null;
  };

  ws.onerror = () => {
    ws.close();
  };

  return new Promise((resolve) => {
    wsResolve = resolve;
  });
}

export function disconnectWebSocket() {
  if (ws) {
    ws.close();
    ws = null;
  }
}

export async function fetchJson(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = await res.json();
  if (!res.ok) {
    throw new Error(body.error || `request failed: ${res.status}`);
  }
  return body;
}

export const api = {
  async health() {
    return fetchJson('/health');
  },

  async state() {
    return fetchJson('/state');
  },

  async startListening() {
    return fetchJson('/listen/start', { method: 'POST' });
  },

  async stopListening() {
    return fetchJson('/listen/stop', { method: 'POST' });
  },

  async sendCommand(text) {
    return fetchJson('/command', {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
  },
};
