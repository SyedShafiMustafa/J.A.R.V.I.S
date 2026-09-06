const API_BASE = '/api';

let ws = null;
let wsResolve = null;

export function createWsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  // Through the Vite dev-server proxy, /ws routes to the backend's
  // WebSocket endpoint on the same origin.
  return `${proto}://${location.host}/ws`;
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
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 30000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
      ...options,
    });
    const body = await res.json();
    if (!res.ok) {
      throw new Error(body.error || `request failed: ${res.status}`);
    }
    return body;
  } finally {
    clearTimeout(timer);
  }
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
