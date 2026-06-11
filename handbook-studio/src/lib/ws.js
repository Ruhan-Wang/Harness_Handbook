// Minimal WebSocket client for streaming generation progress.
let socket = null;
const listeners = new Set();
let connectingPromise = null;

function wsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${window.location.host}/ws`;
}

export function connectWs() {
  if (socket && socket.readyState === WebSocket.OPEN) return Promise.resolve(socket);
  if (connectingPromise) return connectingPromise;
  connectingPromise = new Promise((resolve, reject) => {
    const s = new WebSocket(wsUrl());
    s.onopen = () => {
      socket = s;
      connectingPromise = null;
      resolve(s);
    };
    s.onerror = (e) => {
      connectingPromise = null;
      reject(e);
    };
    s.onclose = () => {
      socket = null;
    };
    s.onmessage = (evt) => {
      let msg;
      try {
        msg = JSON.parse(evt.data);
      } catch {
        return;
      }
      listeners.forEach((fn) => fn(msg));
    };
  });
  return connectingPromise;
}

export function onWsMessage(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export async function sendWs(obj) {
  const s = await connectWs();
  s.send(JSON.stringify(obj));
}
