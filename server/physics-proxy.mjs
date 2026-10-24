import http from 'node:http';

// Fixed loopback target; neither URLs nor destinations come from request input.
export function physicsProxy(req, res) {
  const upstream = http.request({ hostname: '127.0.0.1', port: 8793, path: req.originalUrl,
    method: req.method, headers: { ...req.headers, host: '127.0.0.1:8793' } }, response => {
    res.writeHead(response.statusCode ?? 502, response.headers); response.pipe(res);
  });
  upstream.on('error', () => { if (!res.headersSent) res.status(503).json({ error: 'Start the local physics service on port 8793.' }); else res.destroy(); });
  req.on('aborted', () => upstream.destroy()); req.pipe(upstream);
}

export function physicsUpgrade(req, socket, head) {
  if (req.url !== '/physics/api/stream') { socket.destroy(); return; }
  const upstream = http.request({ hostname: '127.0.0.1', port: 8793, path: req.url,
    headers: { ...req.headers, host: '127.0.0.1:8793' } });
  upstream.on('upgrade', (response, peer, upstreamHead) => {
    socket.write(`HTTP/1.1 101 Switching Protocols\r\n${Object.entries(response.headers).map(([key,value]) => `${key}: ${value}\r\n`).join('')}\r\n`);
    if (head.length) peer.write(head);
    if (upstreamHead.length) socket.write(upstreamHead);
    socket.pipe(peer).pipe(socket);
    socket.on('error', () => peer.destroy()); peer.on('error', () => socket.destroy());
    socket.on('close', () => peer.destroy()); peer.on('close', () => socket.destroy());
  });
  upstream.on('response', () => socket.destroy()); upstream.on('error', () => socket.destroy()); upstream.end();
}
