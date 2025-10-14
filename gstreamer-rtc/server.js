// Simple WebRTC signaling server for Pi-to-Operator connection
// Run with: node signaling_server.js

const WebSocket = require("ws");
const http = require("http");
const express = require("express");

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

// Store connections
const piClients = new Map(); // Raspberry Pi clients
const operatorClients = new Map(); // Operator web clients

// Serve static files for web client
app.use(express.static("public"));

app.get("/", (req, res) => {
  res.send("WebRTC Signaling Server Running");
});

wss.on("connection", (ws, req) => {
  const clientId = generateId();
  let clientType = null;

  console.log(`New connection: ${clientId}`);

  ws.on("message", (message) => {
    try {
      const data = JSON.parse(message);
      console.log(`Message from ${clientId}:`, data.type);

      // Handle registration
      if (data.type === "register") {
        clientType = data.role; // 'pi' or 'operator'

        if (clientType === "pi") {
          piClients.set(clientId, ws);
          console.log(`Pi registered: ${clientId}`);

          // Notify all operators about new Pi
          broadcastToOperators({
            type: "pi_available",
            piId: clientId,
          });
        } else if (clientType === "operator") {
          operatorClients.set(clientId, ws);
          console.log(`Operator registered: ${clientId}`);

          // Send list of available Pis
          const availablePis = Array.from(piClients.keys());
          ws.send(
            JSON.stringify({
              type: "pi_list",
              pis: availablePis,
            })
          );
        }
        return;
      }

      // Handle call initiation from operator
      if (data.type === "start_call") {
        const targetPi = piClients.get(data.piId);
        if (targetPi) {
          // Tell Pi to start call with this operator
          targetPi.send(
            JSON.stringify({
              type: "call_request",
              operatorId: clientId,
            })
          );

          // Store the pairing
          ws.peerId = data.piId;
          targetPi.peerId = clientId;
        }
        return;
      }

      // Forward signaling messages (offer, answer, candidates)
      if (["offer", "answer", "candidate"].includes(data.type)) {
        const peerId = ws.peerId || data.to;

        if (peerId) {
          const peer = piClients.get(peerId) || operatorClients.get(peerId);
          if (peer) {
            // Build payload and include sender id
            const payload = Object.assign({}, data, { from: clientId });
            peer.send(JSON.stringify(payload));

            // Log summary of forwarded payload to help debugging
            let preview = "";
            try {
              if (data.sdp)
                preview = data.sdp.slice(0, 120).replace(/\n/g, "\\n");
              else if (data.candidate)
                preview = JSON.stringify(data.candidate).slice(0, 120);
            } catch (e) {
              preview = "";
            }

            console.log(
              `Forwarded ${data.type} from ${clientId} to ${peerId}` +
                (preview ? ` | preview: ${preview}` : "")
            );
          }
        }
      }
    } catch (error) {
      console.error("Error handling message:", error);
    }
  });

  ws.on("close", () => {
    console.log(`Client disconnected: ${clientId}`);

    // Remove from appropriate map
    piClients.delete(clientId);
    operatorClients.delete(clientId);

    // Notify peer if there was an active call
    if (ws.peerId) {
      const peer = piClients.get(ws.peerId) || operatorClients.get(ws.peerId);
      if (peer) {
        peer.send(
          JSON.stringify({
            type: "peer_disconnected",
            peerId: clientId,
          })
        );
      }
    }

    // Notify operators about Pi disconnection
    if (clientType === "pi") {
      broadcastToOperators({
        type: "pi_disconnected",
        piId: clientId,
      });
    }
  });

  ws.on("error", (error) => {
    console.error(`WebSocket error for ${clientId}:`, error);
  });
});

function broadcastToOperators(message) {
  const msg = JSON.stringify(message);
  operatorClients.forEach((ws) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(msg);
    }
  });
}

function generateId() {
  return Math.random().toString(36).substring(2, 15);
}

const PORT = process.env.PORT || 8081;
server.listen(PORT, () => {
  console.log(`Signaling server listening on port ${PORT}`);
  console.log(`WebSocket: ws://localhost:${PORT}`);
});
