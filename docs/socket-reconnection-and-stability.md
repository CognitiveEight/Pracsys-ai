# Socket.IO connection stability and reconnection

## Why the AI client may disconnect

Disconnects often happen when:

1. **Long-running work** – Strategy and analysis report generation can take 20–60+ seconds. If the **server** uses a short `pingTimeout` (e.g. 20s), it may close the connection before the AI responds to pings.
2. **Server/load balancer** – Proxies or the app server may close idle connections.
3. **Network** – Transient network issues.

## What we do on the AI service (client)

- **Reconnection** is always on (`socket_reconnection=True`).
- **Infinite reconnect attempts** (`socket_reconnection_attempts=0`).
- **Delay before first reconnect** – 2 seconds by default (`socket_reconnection_delay=2000` ms) to avoid tight reconnect loops.
- **Capped backoff** – `socket_reconnection_delay_max=60000` ms (60s) so we don’t wait too long between retries.

Env vars (optional overrides in `.env`):

- `SOCKET_RECONNECTION_DELAY` – ms before first reconnect (default `2000`).
- `SOCKET_RECONNECTION_DELAY_MAX` – max ms between reconnects (default `60000`).

So whenever the connection drops, the client will reconnect automatically after the configured delay.

## What the backend (server) should do to reduce disconnects

If disconnects happen **during** long AI processing (e.g. right after workspace_sync or before “Analysis report sent”):

1. **Increase Socket.IO server timeouts** so the connection is not closed while the AI is busy (e.g. waiting for Gemini). For example, in Node.js:

   ```js
   const io = require('socket.io')(httpServer, {
     pingTimeout: 60000,   // 60 seconds (default often 20000)
     pingInterval: 25000,  // 25 seconds
   });
   ```

2. **Increase any proxy/load balancer idle timeouts** (e.g. nginx, Cloud Run) to at least 60–120 seconds so they don’t close the WebSocket during long requests.

With a higher `pingTimeout`, the server will wait longer for the client (AI service) to respond to pings before closing the connection, so the socket stays up during long AI work.

## document_feature_response: strategy_objective and analysis_report

The AI emits both features the **same way** so the backend can handle them identically:

- **Event:** `document_feature_response`
- **Payload shape:** `{ featureType, workspaceId, userId, payload }`
- **featureType:** `"strategy_objective"` or `"analysis_report"`

Ensure the server listens for `document_feature_response` and persists/uses the `payload` for both `featureType` values (e.g. store or display analysis_report the same way as strategy_objective).
