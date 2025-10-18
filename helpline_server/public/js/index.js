// Global state
let ws = null;
let authToken = null;
let username = null;
let pc = null;
let currentPeerId = null;
let callStartTime = null;
let callDurationInterval = null;
let micStream = null;
let autoScroll = true;
let logCount = 0;

// Audio playback
let audioElement = null;
let audioQueue = [];
let isPlayingAudio = false;

const WS_URL = `ws://${window.location.host}`;
const API_URL = `http://${window.location.host}`;

// Debug logging function
function debugLog(message, type = "info") {
  const timestamp = new Date().toLocaleTimeString();
  const logsDiv = document.getElementById("debugLogs");
  const logEntry = document.createElement("div");
  logEntry.className = "mb-1 break-words text-sm";

  const colorMap = {
    info: "text-blue-300",
    success: "text-green-300",
    warning: "text-yellow-300",
    error: "text-red-300",
    debug: "text-indigo-300",
    webrtc: "text-yellow-200",
    audio: "text-teal-200",
  };

  const colorClass = colorMap[type] || "text-gray-300";
  logEntry.innerHTML = `<span class="text-gray-400 mr-2">[${timestamp}]</span><span class="${colorClass}">${message}</span>`;
  logsDiv.appendChild(logEntry);

  logCount++;
  document.getElementById("logCount").textContent = logCount;

  if (autoScroll) {
    logsDiv.scrollTop = logsDiv.scrollHeight;
  }

  console.log(`[${type.toUpperCase()}] ${message}`);
}

function clearLogs() {
  document.getElementById("debugLogs").innerHTML = "";
  logCount = 0;
  document.getElementById("logCount").textContent = logCount;
  debugLog("Debug console cleared", "info");
}

function toggleAutoScroll() {
  autoScroll = !autoScroll;
  document.getElementById("autoScrollBtn").textContent = `Auto-scroll: ${
    autoScroll ? "ON" : "OFF"
  }`;
  debugLog(`Auto-scroll ${autoScroll ? "enabled" : "disabled"}`, "info");
}

if (localStorage.getItem("authToken")) {
  authToken = localStorage.getItem("authToken");
  username = localStorage.getItem("username");
  showDashboard();
  connectWebSocket();
}

// Login
document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();

  const usernameInput = document.getElementById("username").value;
  const passwordInput = document.getElementById("password").value;
  const loginBtn = document.getElementById("loginBtnText");

  loginBtn.innerHTML = '<span class="loading"></span> Logging in...';
  debugLog(`Attempting login as ${usernameInput}`, "info");

  try {
    const response = await fetch(`${API_URL}/api/operator/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: usernameInput,
        password: passwordInput,
      }),
    });

    const data = await response.json();

    if (response.ok) {
      authToken = data.token;
      username = data.username;
      showDashboard();
      localStorage.setItem("authToken", authToken);
      localStorage.setItem("username", username);
      debugLog(`Login successful as ${username}`, "success");
      connectWebSocket();
    } else {
      debugLog(`Login failed: ${data.error || "Unknown error"}`, "error");
      showAlert("loginAlert", data.error || "Login failed", "error");
    }
  } catch (error) {
    debugLog(`Login connection error: ${error.message}`, "error");
    showAlert("loginAlert", "Connection error. Please try again.", "error");
  } finally {
    loginBtn.textContent = "Login";
  }
});

function showDashboard() {
  document.getElementById("loginSection").classList.add("hidden");
  document.getElementById("dashboard").classList.remove("hidden");
  document.getElementById("usernameDisplay").textContent = username;
}

function logout() {
  debugLog("Logging out", "info");
  if (ws) ws.close();
  if (pc) pc.close();
  if (audioElement) {
    audioElement.pause();
    audioElement.src = "";
  }
  authToken = null;
  username = null;
  localStorage.removeItem("authToken");
  localStorage.removeItem("username");
  document.getElementById("loginSection").classList.remove("hidden");
  document.getElementById("dashboard").classList.add("hidden");
  document.getElementById("username").value = "";
  document.getElementById("password").value = "";
}

// WebSocket connection
function connectWebSocket() {
  debugLog(`Connecting to WebSocket: ${WS_URL}`, "info");
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    debugLog("WebSocket connection established", "success");
    const status = document.getElementById("statusIndicator");
    status.classList.remove("bg-red-500");
    status.classList.add("bg-green-500");

    ws.send(
      JSON.stringify({
        type: "authenticate",
        token: authToken,
      })
    );
    debugLog("Sent authentication token", "info");
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    handleMessage(data);
  };

  ws.onclose = () => {
    debugLog("WebSocket disconnected - reconnecting in 3s", "warning");
    const status = document.getElementById("statusIndicator");
    status.classList.remove("bg-green-500");
    status.classList.add("bg-red-500");
    setTimeout(connectWebSocket, 3000);
  };

  ws.onerror = (error) => {
    debugLog(`WebSocket error: ${error}`, "error");
  };
}

async function handleMessage(data) {
  switch (data.type) {
    case "authenticated":
      debugLog("WebSocket authenticated successfully", "success");
      break;

    case "pi_list":
      debugLog(`Received device list: ${data.pis.length} device(s)`, "info");
      updateDeviceList(data.pis);
      break;

    case "pi_available":
      debugLog(`Device available: ${data.piId.slice(0, 8)}`, "success");
      addDevice(data.piId, data.publicKey);
      break;

    case "pi_disconnected":
      debugLog(`Device disconnected: ${data.piId.slice(0, 8)}`, "warning");
      removeDevice(data.piId);
      break;

    case "queue_update":
      debugLog(`Queue updated: ${data.queue.length} call(s) waiting`, "info");
      updateQueue(data.queue);
      break;

    case "offer":
      debugLog(
        `📞 Received WebRTC offer from ${data.from.slice(0, 8)}`,
        "webrtc"
      );
      await handleOffer(data.sdp, data.from);
      break;

    case "answer":
      debugLog(`📞 Received WebRTC answer`, "webrtc");
      await handleAnswer(data.sdp);
      break;

    case "candidate":
      debugLog(`📞 Received ICE candidate`, "debug");
      await handleCandidate(data.candidate);
      break;

    case "peer_disconnected":
      debugLog("Peer disconnected", "warning");
      handlePeerDisconnected();
      break;

    case "error":
      debugLog(`Server error: ${data.message}`, "error");
      break;
  }
}

function updateDeviceList(devices) {
  const list = document.getElementById("deviceList");
  list.innerHTML = "";

  if (devices.length === 0) {
    list.innerHTML = `
            <li class="empty-state text-center text-gray-400 py-8">
              <svg class="mx-auto mb-2 w-16 h-16 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path>
              </svg>
              <p>No devices online</p>
            </li>
          `;
  } else {
    devices.forEach((device) => {
      addDevice(device.piId, device.publicKey);
    });
  }

  updateStats();
}

function addDevice(piId, publicKey) {
  const list = document.getElementById("deviceList");
  const emptyState = list.querySelector(".empty-state");
  if (emptyState) emptyState.remove();

  if (document.getElementById(`device-${piId}`)) return;

  const li = document.createElement("li");
  li.className =
    "flex items-center justify-between border border-gray-100 rounded-lg p-3 hover:border-indigo-300";
  li.id = `device-${piId}`;
  li.innerHTML = `
          <div class="flex-1">
            <div class="font-semibold text-gray-700">Device ${piId.slice(
              0,
              8
            )}</div>
            <div class="text-xs text-gray-500">${
              publicKey ? publicKey.slice(0, 20) + "..." : piId
            }</div>
          </div>
          <span class="inline-block bg-green-100 text-green-800 text-xs font-semibold px-3 py-1 rounded-full">Online</span>
        `;
  list.appendChild(li);
  updateStats();
}

function removeDevice(piId) {
  const element = document.getElementById(`device-${piId}`);
  if (element) element.remove();

  const list = document.getElementById("deviceList");
  if (list.children.length === 0) {
    list.innerHTML = `
            <li class="empty-state text-center text-gray-400 py-8">
              <p>No devices online</p>
            </li>
          `;
  }
  updateStats();
}

function updateQueue(queue) {
  const list = document.getElementById("queueList");
  list.innerHTML = "";

  if (queue.length === 0) {
    list.innerHTML = `
            <li class="empty-state text-center text-gray-400 py-8">
              <svg class="mx-auto mb-2 w-16 h-16 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z"></path>
              </svg>
              <p>No pending calls</p>
            </li>
          `;
  } else {
    queue.forEach((call) => {
      const li = document.createElement("li");
      li.className =
        "flex items-center justify-between border border-gray-100 rounded-lg p-3 hover:border-indigo-300";
      const timeAgo = getTimeAgo(call.timestamp);
      li.innerHTML = `
              <div>
                <div class="font-semibold text-gray-700">Device ${call.piId.slice(
                  0,
                  8
                )}</div>
                <div class="text-xs text-gray-500">${timeAgo}</div>
              </div>
              <button class="bg-green-500 hover:bg-green-600 text-white px-3 py-1 rounded-md" onclick="takeCall('${
                call.piId
              }')">Accept</button>
            `;
      list.appendChild(li);
    });
  }

  updateStats();
}

function getTimeAgo(timestamp) {
  const seconds = Math.floor((Date.now() - timestamp) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ago`;
}

function updateStats() {
  const deviceList = document.getElementById("deviceList");
  const queueList = document.getElementById("queueList");

  const deviceCount = Array.from(deviceList.children).filter(
    (el) => !el.classList.contains("empty-state")
  ).length;
  const queueCount = Array.from(queueList.children).filter(
    (el) => !el.classList.contains("empty-state")
  ).length;

  document.getElementById("deviceCount").textContent = deviceCount;
  document.getElementById("queueCount").textContent = queueCount;
  document.getElementById("deviceBadge").textContent = deviceCount;
  document.getElementById("queueBadge").textContent = queueCount;
}

async function takeCall(piId) {
  debugLog(`📞 Taking call from device ${piId.slice(0, 8)}`, "success");
  currentPeerId = piId;

  // Get microphone access
  try {
    debugLog("🎤 Requesting microphone access...", "audio");
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    debugLog("✅ Microphone access granted", "success");
    // Reset mute state and enable mute button in UI
    isMuted = false;
    const muteBtn = document.getElementById("muteBtn");
    if (muteBtn) {
      muteBtn.textContent = "Mute";
      muteBtn.disabled = false;
    }
  } catch (err) {
    debugLog(`❌ Microphone access denied: ${err.message}`, "error");
    alert("Failed to access microphone. Please grant permission.");
    return;
  }

  // Send take_call message
  ws.send(
    JSON.stringify({
      type: "take_call",
      piId: piId,
    })
  );
  debugLog("⬆️ Sent take_call message", "info");

  // Create peer connection for video only (NO AUDIO TRACKS)
  await createPeerConnection();

  // Attach local microphone tracks to peer connection
  try {
    ensureLocalAudioTracks();
  } catch (err) {
    debugLog(`Could not attach local audio tracks: ${err.message}`, "error");
  }

  // Show video container
  document.getElementById("videoContainer").classList.remove("hidden");
  document.getElementById("callDeviceId").textContent = piId.slice(0, 8);
  document.getElementById("callStatus").textContent = "In Call";

  // Start call timer
  callStartTime = Date.now();
  callDurationInterval = setInterval(updateCallDuration, 1000);
}

async function createPeerConnection() {
  debugLog("🔗 Creating RTCPeerConnection (video+audio)", "webrtc");
  const configuration = {
    iceServers: [
      { urls: "stun:stun.l.google.com:19302" },
      { urls: "stun:stun1.l.google.com:19302" },
    ],
  };

  pc = new RTCPeerConnection(configuration);
  debugLog("✅ RTCPeerConnection created", "webrtc");

  // Handle incoming tracks (video AND audio)
  pc.ontrack = (event) => {
    debugLog(`📥 Received remote ${event.track.kind} track`, "webrtc");
    if (event.track.kind === "video") {
      const video = document.getElementById("remoteVideo");
      video.srcObject = event.streams[0];
      debugLog("📹 Remote video stream connected", "webrtc");
    } else if (event.track.kind === "audio") {
      // Create audio element for remote audio
      if (!audioElement) {
        audioElement = new Audio();
        audioElement.autoplay = true;
      }
      audioElement.srcObject = event.streams[0];
      debugLog("🔊 Remote audio stream connected", "webrtc");
    }
  };

  pc.onicecandidate = (event) => {
    if (event.candidate) {
      debugLog(`📤 Sending ICE candidate: ${event.candidate.type}`, "debug");
      ws.send(
        JSON.stringify({
          type: "candidate",
          candidate: {
            candidate: event.candidate.candidate,
            sdpMLineIndex: event.candidate.sdpMLineIndex,
          },
          to: currentPeerId,
        })
      );
    } else {
      debugLog("✅ ICE candidate gathering complete", "webrtc");
    }
  };

  pc.onconnectionstatechange = () => {
    debugLog(`🔗 Connection state: ${pc.connectionState}`, "webrtc");
    document.getElementById("connectionState").textContent = pc.connectionState;

    if (pc.connectionState === "connected") {
      debugLog("✅ Peer connection established successfully", "success");
    } else if (pc.connectionState === "failed") {
      debugLog("❌ Peer connection failed", "error");
    } else if (pc.connectionState === "disconnected") {
      debugLog("⚠️ Peer connection disconnected", "warning");
    }
  };

  pc.oniceconnectionstatechange = () => {
    debugLog(`🧊 ICE connection state: ${pc.iceConnectionState}`, "webrtc");
  };

  pc.onicegatheringstatechange = () => {
    debugLog(`🧊 ICE gathering state: ${pc.iceGatheringState}`, "webrtc");
  };

  pc.onsignalingstatechange = () => {
    debugLog(`📡 Signaling state: ${pc.signalingState}`, "webrtc");
  };
}

async function handleOffer(sdp, from) {
  if (!pc) {
    debugLog("Creating peer connection to handle offer", "webrtc");
    await createPeerConnection();
  }

  currentPeerId = from;

  debugLog("Setting remote description (offer)", "webrtc");
  await pc.setRemoteDescription(
    new RTCSessionDescription({
      type: "offer",
      sdp: sdp,
    })
  );

  // Ensure local microphone tracks are attached before creating an answer
  try {
    ensureLocalAudioTracks();
  } catch (err) {
    debugLog(`Failed to attach local audio tracks: ${err.message}`, "error");
  }

  debugLog("Creating answer", "webrtc");
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  debugLog("Local description set (answer)", "webrtc");

  ws.send(
    JSON.stringify({
      type: "answer",
      sdp: answer.sdp,
      to: from,
    })
  );
  debugLog("📤 Answer sent to device", "webrtc");
}

async function handleAnswer(sdp) {
  if (pc) {
    debugLog("Setting remote description (answer)", "webrtc");
    await pc.setRemoteDescription(
      new RTCSessionDescription({
        type: "answer",
        sdp: sdp,
      })
    );
    debugLog("✅ Remote description set", "webrtc");
  }
}

async function handleCandidate(candidate) {
  if (pc && candidate) {
    await pc.addIceCandidate(
      new RTCIceCandidate({
        candidate: candidate.candidate,
        sdpMLineIndex: candidate.sdpMLineIndex,
      })
    );
  }
}

function handlePeerDisconnected() {
  debugLog("Peer disconnected - ending call", "warning");
  endCall();
}

function endCall() {
  debugLog("📴 Ending call", "info");

  if (pc) {
    debugLog("Closing peer connection", "webrtc");
    pc.close();
    pc = null;
  }

  // Stop microphone
  if (micStream) {
    debugLog("Stopping microphone stream", "audio");
    micStream.getTracks().forEach((track) => track.stop());
    micStream = null;
  }

  // Clean up audio playback
  if (audioElement) {
    audioElement.pause();
    audioElement.src = "";
    audioElement = null;
  }
  audioQueue.forEach((url) => URL.revokeObjectURL(url));
  audioQueue = [];
  isPlayingAudio = false;

  if (callDurationInterval) {
    clearInterval(callDurationInterval);
    callDurationInterval = null;
  }

  document.getElementById("videoContainer").classList.add("hidden");
  document.getElementById("remoteVideo").srcObject = null;
  document.getElementById("callStatus").textContent = "Idle";
  currentPeerId = null;
  callStartTime = null;

  // Reset mute UI
  isMuted = false;
  const muteBtn = document.getElementById("muteBtn");
  if (muteBtn) {
    muteBtn.textContent = "Mute";
    muteBtn.disabled = true;
  }
  // send websocket message to end call on server side
  ws.send(
    JSON.stringify({
      type: "end_call",
      piId: currentPeerId,
    })
  );
  debugLog("✅ Call cleanup complete", "success");
}

// Ensure local microphone tracks are added to the peer connection once
function ensureLocalAudioTracks() {
  if (!pc) {
    throw new Error("PeerConnection is not initialized");
  }

  if (!micStream) {
    // micStream should already be created by takeCall, but guard anyway
    throw new Error("No microphone stream available");
  }

  // Add each audio track only once
  micStream.getAudioTracks().forEach((track) => {
    // Check if this track is already added by comparing track ids
    const alreadyAdded = pc.getSenders().some((sender) => {
      return sender.track && sender.track.id === track.id;
    });

    if (!alreadyAdded) {
      pc.addTrack(track, micStream);
      debugLog("🔼 Added local audio track to PeerConnection", "audio");
    } else {
      debugLog("ℹ️ Local audio track already added to PeerConnection", "audio");
    }
  });
}

// Optional helpers for mute/unmute
function muteLocalMic() {
  if (micStream) micStream.getAudioTracks().forEach((t) => (t.enabled = false));
}

function unmuteLocalMic() {
  if (micStream) micStream.getAudioTracks().forEach((t) => (t.enabled = true));
}

// UI-facing mute toggle used by the Mute button
let isMuted = false;
function toggleMute() {
  if (!micStream) {
    debugLog("No local microphone stream to mute/unmute", "warning");
    return;
  }

  isMuted = !isMuted;
  if (isMuted) {
    muteLocalMic();
    debugLog("Microphone muted", "audio");
  } else {
    unmuteLocalMic();
    debugLog("Microphone unmuted", "audio");
  }

  // Update UI button and indicator
  const muteBtn = document.getElementById("muteBtn");
  if (muteBtn) muteBtn.textContent = isMuted ? "Unmute" : "Mute";

  const audioIndicator = document.getElementById("audioIndicator");
  if (audioIndicator) {
    audioIndicator.textContent = isMuted
      ? "🔇 Muted"
      : "🔊 WebSocket Streaming";
    audioIndicator.className = isMuted
      ? "inline-flex items-center px-3 py-1 rounded-md bg-red-50 text-red-700 text-sm"
      : "inline-flex items-center px-3 py-1 rounded-md bg-green-50 text-green-700 text-sm";
  }
}

function updateCallDuration() {
  if (!callStartTime) return;

  const elapsed = Math.floor((Date.now() - callStartTime) / 1000);
  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  document.getElementById("callDuration").textContent = `${String(
    minutes
  ).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function showAlert(elementId, message, type) {
  const alertDiv = document.getElementById(elementId);
  const typeMap = {
    error: "bg-red-100 text-red-700 border-red-200",
    success: "bg-green-100 text-green-700 border-green-200",
    info: "bg-blue-100 text-blue-700 border-blue-200",
  };
  const classes = typeMap[type] || typeMap.info;
  alertDiv.innerHTML = `
          <div class="border px-4 py-2 rounded-md ${classes}">
            ${message}
          </div>
        `;
  setTimeout(() => {
    alertDiv.innerHTML = "";
  }, 5000);
}
