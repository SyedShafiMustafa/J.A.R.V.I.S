const BASE = '/api';

export const api = {
  async health() {
    const res = await fetch(`${BASE}/health`);
    if (!res.ok) {
      throw new Error(`health check failed: ${res.status}`);
    }
    return res.json();
  },

  async sendTranscript(text) {
    const res = await fetch(`${BASE}/transcript`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      throw new Error(`transcript request failed: ${res.status}`);
    }
    return res.json();
  },

  async sendCommand(command) {
    const res = await fetch(`${BASE}/command`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command }),
    });
    if (!res.ok) {
      throw new Error(`command request failed: ${res.status}`);
    }
    return res.json();
  },
};
