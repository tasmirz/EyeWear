// WebRTC Signaling Server with WebSocket Audio
// Video: WebRTC, Audio: WebSocket
// Run with: node server.js

const express = require("express");
const http = require("http");
const WebSocket = require("ws");
const mongoose = require("mongoose");
const jwt = require("jsonwebtoken");
const crypto = require("crypto");
const bcrypt = require("bcrypt");
const dotenv = require("dotenv");

dotenv.config();

// Configuration
const PORT = process.env.PORT || 8081;
const MONGODB_URI =
  process.env.MONGODB_URI || "mongodb://localhost:27017/ocr_system";
const JWT_SECRET =
  process.env.JWT_SECRET || "your-secret-key-change-in-production";
const CHALLENGE_EXPIRY = 5 * 60 * 1000; // 5 minutes

// Initialize Express
const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.use(express.json());
app.use(express.static("public"));

// MongoDB Schemas
const deviceSchema = new mongoose.Schema({
  fingerprint: { type: String, required: true, unique: true },
  public_key: { type: String, required: true, unique: true },
  status: {
    type: String,
    enum: ["active", "inactive"],
    default: "inactive",
    required: false,
  },
  authorized: { type: Boolean, default: false },
  first_seen: { type: Date, default: Date.now },
  last_seen: { type: Date, default: Date.now },
});

const operatorSchema = new mongoose.Schema({
  username: { type: String, required: true, unique: true },
  password: { type: String, required: true },
  role: { type: String, enum: ["admin", "operator"], default: "operator" },
  createdAt: { type: Date, default: Date.now },
});

const Device = mongoose.model("Device", deviceSchema);
const Operator = mongoose.model("Operator", operatorSchema);

// Connect to MongoDB
mongoose
  .connect(MONGODB_URI)
  .then(() => {
    console.log("✅ Connected to MongoDB");
    initializeAdmin();
  })
  .catch((err) => console.error("❌ MongoDB connection error:", err));

// Initialize default admin user
async function initializeAdmin() {
  try {
    const adminExists = await Operator.findOne({ username: "admin" });
    if (!adminExists) {
      const hashedPassword = await bcrypt.hash("admin", 10);
      await Operator.create({
        username: "admin",
        password: hashedPassword,
        role: "admin",
      });
      console.log("✅ Default admin user created (admin/admin)");
    }
  } catch (error) {
    console.error("Error initializing admin:", error);
  }
}

// Authentication challenges store (in-memory)
const authChallenges = new Map();

// Call queue
const callQueue = [];

// WebSocket clients
const piClients = new Map();
const operatorClients = new Map();

// ==================== REST API ENDPOINTS ====================

// Pi Device Authentication - Step 1: Get Challenge
app.post("/api/challenge", async (req, res) => {
  try {
    const { publicKey } = req.body;
    console.log("Challenge request for publicKey:", publicKey);
    if (!publicKey) {
      return res.status(400).json({ error: "Public key required" });
    }

    const device = await Device.findOne({ public_key: publicKey });
    console.log("Device found:", device);
    if (!device) {
      return res
        .status(403)
        .json({ error: "Device not registered or inactive" });
    }

    const challengeText = crypto.randomBytes(32).toString("hex");
    const challengeToken = jwt.sign(
      { publicKey, challenge: challengeText },
      JWT_SECRET,
      { expiresIn: "5m" }
    );

    authChallenges.set(publicKey, {
      challenge: challengeText,
      timestamp: Date.now(),
    });

    setTimeout(() => authChallenges.delete(publicKey), CHALLENGE_EXPIRY);

    res.json({ challengeToken, challengeText });
  } catch (error) {
    console.error("Challenge error:", error);
    res.status(500).json({ error: "Internal server error" });
  }
});

// Pi Device Authentication - Step 2: Verify Signature
app.post("/api/auth", async (req, res) => {
  try {
    const { challengeToken, signedChallenge } = req.body;

    if (!challengeToken || !signedChallenge) {
      return res.status(400).json({ error: "Missing required fields" });
    }

    let decoded;
    try {
      decoded = jwt.verify(challengeToken, JWT_SECRET);
    } catch (err) {
      return res
        .status(401)
        .json({ error: "Invalid or expired challenge token" });
    }

    const { publicKey, challenge } = decoded;

    const storedChallenge = authChallenges.get(publicKey);
    if (!storedChallenge || storedChallenge.challenge !== challenge) {
      return res.status(401).json({ error: "Challenge expired or invalid" });
    }

    authChallenges.delete(publicKey);

    await Device.findOneAndUpdate(
      { public_key: publicKey },
      { last_seen: Date.now() }
    );

    const finalToken = jwt.sign(
      { public_key: publicKey, type: "device" },
      JWT_SECRET,
      { expiresIn: "24h" }
    );

    res.json({
      token: finalToken,
      message: "Authentication successful",
    });
  } catch (error) {
    console.error("Auth error:", error);
    res.status(500).json({ error: "Internal server error" });
  }
});

// Operator Login
app.post("/api/operator/login", async (req, res) => {
  try {
    const { username, password } = req.body;

    if (!username || !password) {
      return res.status(400).json({ error: "Username and password required" });
    }

    const operator = await Operator.findOne({ username });
    if (!operator) {
      return res.status(401).json({ error: "Invalid credentials" });
    }

    const isValidPassword = await bcrypt.compare(password, operator.password);
    if (!isValidPassword) {
      return res.status(401).json({ error: "Invalid credentials" });
    }

    const token = jwt.sign(
      { username, role: operator.role, type: "operator" },
      JWT_SECRET,
      { expiresIn: "8h" }
    );

    res.json({ token, role: operator.role, username });
  } catch (error) {
    console.error("Operator login error:", error);
    res.status(500).json({ error: "Internal server error" });
  }
});

// Admin - Add Operator
app.post(
  "/api/admin/operators",
  authenticateToken,
  requireAdmin,
  async (req, res) => {
    try {
      const { username, password, role = "operator" } = req.body;

      if (!username || !password) {
        return res
          .status(400)
          .json({ error: "Username and password required" });
      }

      const exists = await Operator.findOne({ username });
      if (exists) {
        return res.status(409).json({ error: "Username already exists" });
      }

      const hashedPassword = await bcrypt.hash(password, 10);
      const operator = await Operator.create({
        username,
        password: hashedPassword,
        role,
      });

      res.status(201).json({
        message: "Operator created successfully",
        operator: { username: operator.username, role: operator.role },
      });
    } catch (error) {
      console.error("Create operator error:", error);
      res.status(500).json({ error: "Internal server error" });
    }
  }
);

// Delete Operator (Admin)
app.delete(
  "/api/admin/operators/:id",
  authenticateToken,
  requireAdmin,
  async (req, res) => {
    try {
      const { id } = req.params;
      const operator = await Operator.findById(id);
      if (!operator) {
        return res.status(404).json({ error: "Operator not found" });
      }
      if (operator.username === "admin") {
        return res.status(403).json({ error: "Cannot delete default admin" });
      }
      await Operator.findByIdAndDelete(id);
      res.json({ message: "Operator deleted successfully" });
    } catch (error) {
      console.error("Delete operator error:", error);
      res.status(500).json({ error: "Internal server error" });
    }
  }
);

// Delete Device (Admin)
app.delete(
  "/api/admin/devices/:id",
  authenticateToken,
  requireAdmin,
  async (req, res) => {
    try {
      const { id } = req.params;
      const device = await Device.findById(id);
      if (!device) {
        return res.status(404).json({ error: "Device not found" });
      }
      await Device.findByIdAndDelete(id);
      res.json({ message: "Device deleted successfully" });
    } catch (error) {
      console.error("Delete device error:", error);
      res.status(500).json({ error: "Internal server error" });
    }
  }
);

// Admin - Add Device
app.post(
  "/api/admin/devices",
  authenticateToken,
  requireAdmin,
  async (req, res) => {
    try {
      const { publicKey, deviceName } = req.body;

      if (!publicKey || !deviceName) {
        return res
          .status(400)
          .json({ error: "Public key and device name required" });
      }

      const exists = await Device.findOne({ public_key: publicKey });
      if (exists) {
        return res.status(409).json({ error: "Device already exists" });
      }

      const device = await Device.create({
        public_key: publicKey,
        fingerprint: deviceName,
      });

      res.status(201).json({
        message: "Device registered successfully",
        device: {
          public_key: device.public_key,
          fingerprint: device.fingerprint,
        },
      });
    } catch (error) {
      console.error("Create device error:", error);
      res.status(500).json({ error: "Internal server error" });
    }
  }
);

// Get all devices (Admin)
app.get(
  "/api/admin/devices",
  authenticateToken,
  requireAdmin,
  async (req, res) => {
    try {
      const devices = await Device.find().select("-__v");
      res.json({ devices });
    } catch (error) {
      console.error("Get devices error:", error);
      res.status(500).json({ error: "Internal server error" });
    }
  }
);

// Get all operators (Admin)
app.get(
  "/api/admin/operators",
  authenticateToken,
  requireAdmin,
  async (req, res) => {
    try {
      const operators = await Operator.find().select("-password -__v");
      res.json({ operators });
    } catch (error) {
      console.error("Get operators error:", error);
      res.status(500).json({ error: "Internal server error" });
    }
  }
);

// Get call queue
app.get("/api/queue", authenticateToken, (req, res) => {
  res.json({ queue: callQueue });
});

// Middleware
function authenticateToken(req, res, next) {
  const authHeader = req.headers["authorization"];
  const token = authHeader && authHeader.split(" ")[1];

  if (!token) {
    return res.status(401).json({ error: "Access token required" });
  }

  jwt.verify(token, JWT_SECRET, (err, user) => {
    if (err) {
      return res.status(403).json({ error: "Invalid or expired token" });
    }
    req.user = user;
    next();
  });
}

function requireAdmin(req, res, next) {
  if (req.user.role !== "admin") {
    return res.status(403).json({ error: "Admin access required" });
  }
  next();
}

// ==================== WEBSOCKET HANDLING ====================

wss.on("connection", (ws, req) => {
  const clientId = generateId();
  let clientType = null;
  let authenticated = false;

  console.log(`New WebSocket connection: ${clientId}`);

  ws.on("message", async (message) => {
    try {
      const data = JSON.parse(message);

      // Authentication required first
      if (data.type === "authenticate") {
        const { token } = data;

        try {
          const decoded = jwt.verify(token, JWT_SECRET);
          authenticated = true;
          clientType = decoded.type;

          if (clientType === "device") {
            piClients.set(clientId, {
              ws,
              publicKey: decoded.publicKey,
              deviceId: clientId,
            });
            console.log(`✅ Device authenticated: ${clientId}`);

            ws.send(
              JSON.stringify({
                type: "authenticated",
                deviceId: clientId,
              })
            );

            // Notify operators
            broadcastToOperators({
              type: "pi_available",
              piId: clientId,
              publicKey: decoded.publicKey,
            });
          } else if (clientType === "operator") {
            operatorClients.set(clientId, {
              ws,
              username: decoded.username,
              role: decoded.role,
            });
            console.log(`✅ Operator authenticated: ${decoded.username}`);

            ws.send(
              JSON.stringify({
                type: "authenticated",
                operatorId: clientId,
              })
            );

            // Send available devices
            const availablePis = Array.from(piClients.entries()).map(
              ([id, client]) => ({
                piId: id,
                publicKey: client.publicKey,
              })
            );

            ws.send(
              JSON.stringify({
                type: "pi_list",
                pis: availablePis,
              })
            );

            // Send current queue
            ws.send(
              JSON.stringify({
                type: "queue_update",
                queue: callQueue,
              })
            );
          }
        } catch (err) {
          ws.send(
            JSON.stringify({
              type: "error",
              message: "Authentication failed",
            })
          );
          ws.close();
        }
        return;
      }

      if (!authenticated) {
        ws.send(
          JSON.stringify({
            type: "error",
            message: "Not authenticated",
          })
        );
        return;
      }

      // Request call (Pi device)
      if (data.type === "request_call" && clientType === "device") {
        const piClient = piClients.get(clientId);
        const callRequest = {
          piId: clientId,
          publicKey: piClient.publicKey,
          timestamp: Date.now(),
        };

        callQueue.push(callRequest);
        console.log(`📞 Call request added to queue from device ${clientId}`);

        // Notify all operators
        broadcastToOperators({
          type: "queue_update",
          queue: callQueue,
        });

        ws.send(
          JSON.stringify({
            type: "call_queued",
            position: callQueue.length,
          })
        );
        return;
      }

      // Take call (Operator)
      if (data.type === "take_call" && clientType === "operator") {
        const { piId } = data;

        // Remove from queue
        const index = callQueue.findIndex((req) => req.piId === piId);
        if (index !== -1) {
          callQueue.splice(index, 1);
        }

        const targetPi = piClients.get(piId);
        if (targetPi) {
          // Notify Pi device
          targetPi.ws.send(
            JSON.stringify({
              type: "call_accepted",
              operatorId: clientId,
            })
          );

          // Store pairing
          ws.peerId = piId;
          targetPi.ws.peerId = clientId;

          console.log(`📞 Call established: ${piId} <-> ${clientId}`);

          // Update all operators about queue change
          broadcastToOperators({
            type: "queue_update",
            queue: callQueue,
          });
        }
        return;
      }

      // Handle audio data routing
      if (data.type === "audio_data") {
        const peerId = ws.peerId || data.to;
        if (peerId) {
          const peer =
            piClients.get(peerId)?.ws || operatorClients.get(peerId)?.ws;
          if (peer && peer.readyState === WebSocket.OPEN) {
            peer.send(
              JSON.stringify({
                type: "audio_data",
                data: data.data,
                from: clientId,
              })
            );
          }
        }
        return;
      }

      // Forward WebRTC signaling (video only)
      if (["offer", "answer", "candidate"].includes(data.type)) {
        const peerId = ws.peerId || data.to;

        if (peerId) {
          const peer =
            piClients.get(peerId)?.ws || operatorClients.get(peerId)?.ws;
          if (peer) {
            const payload = { ...data, from: clientId };
            peer.send(JSON.stringify(payload));
            console.log(`Forwarded ${data.type} from ${clientId} to ${peerId}`);
          }
        }
      }

      // Handle mute status
      if (data.type === "mute_status") {
        const peerId = ws.peerId || data.to;
        if (peerId) {
          const peer =
            piClients.get(peerId)?.ws || operatorClients.get(peerId)?.ws;
          if (peer) {
            peer.send(
              JSON.stringify({
                type: "mute_status",
                muted: data.muted,
                from: clientId,
              })
            );
          }
        }
      }
    } catch (error) {
      console.error("WebSocket message error:", error);
      ws.send(
        JSON.stringify({
          type: "error",
          message: "Internal server error",
        })
      );
    }
  });

  ws.on("close", () => {
    console.log(`Client disconnected: ${clientId}`);

    // Remove from call queue if present
    const queueIndex = callQueue.findIndex((req) => req.piId === clientId);
    if (queueIndex !== -1) {
      callQueue.splice(queueIndex, 1);
      broadcastToOperators({
        type: "queue_update",
        queue: callQueue,
      });
    }

    piClients.delete(clientId);
    operatorClients.delete(clientId);

    // Notify peer
    if (ws.peerId) {
      const peer =
        piClients.get(ws.peerId)?.ws || operatorClients.get(ws.peerId)?.ws;
      if (peer) {
        peer.send(
          JSON.stringify({
            type: "peer_disconnected",
            peerId: clientId,
          })
        );
      }
    }

    // Notify operators about device disconnection
    if (clientType === "device") {
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
  operatorClients.forEach((client) => {
    if (client.ws.readyState === WebSocket.OPEN) {
      client.ws.send(msg);
    }
  });
}

function generateId() {
  return crypto.randomBytes(8).toString("hex");
}

// Start server
server.listen(PORT, () => {
  console.log(`\n🚀 WebRTC Signaling Server with WebSocket Audio`);
  console.log(`   HTTP: http://localhost:${PORT}`);
  console.log(`   WebSocket: ws://localhost:${PORT}`);
  console.log(`   MongoDB: ${MONGODB_URI}`);
  console.log(`   Audio: WebSocket streaming`);
  console.log(`   Video: WebRTC (H.264)\n`);
});
