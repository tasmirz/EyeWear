let authToken = null;
const API_URL = `http://${window.location.host}`;

if (localStorage.getItem("AauthToken")) {
  authToken = localStorage.getItem("AauthToken");
  const username = localStorage.getItem("Ausername");
  document.getElementById("adminName").textContent = username;
  document.getElementById("loginSection").style.display = "none";
  document.getElementById("dashboard").classList.add("active");
  loadOperators();
  loadDevices();
}

// Helper: calls API, attaches Bearer token when present and logs out on 401/403
async function apiFetch(path, options = {}) {
  const url = `${API_URL}${path}`;
  const opts = Object.assign({}, options);
  opts.headers = Object.assign({}, opts.headers || {});

  if (authToken) {
    opts.headers.Authorization = `Bearer ${authToken}`;
  }

  const res = await fetch(url, opts);

  if (res.status === 401 || res.status === 403) {
    // invalid/expired token — clear session and show notice
    try {
      showAlert(
        "loginAlert",
        "Session expired or unauthorized. Please login again.",
        "error"
      );
    } catch (e) {
      // ignore if DOM not ready
    }
    logout();
    throw new Error("unauthorized");
  }

  return res;
}

// Login
document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();

  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;

  try {
    const response = await fetch(`${API_URL}/api/operator/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });

    const data = await response.json();

    if (response.ok) {
      if (data.role !== "admin") {
        showAlert("loginAlert", "Access denied. Admin role required.", "error");
        return;
      }

      authToken = data.token;
      // save to localStorage/sessionStorage if needed
      localStorage.setItem("AauthToken", authToken);
      localStorage.setItem("Ausername", username);

      document.getElementById("adminName").textContent = data.username;
      document.getElementById("loginSection").style.display = "none";
      document.getElementById("dashboard").classList.add("active");

      loadOperators();
      loadDevices();
    } else {
      showAlert("loginAlert", data.error || "Login failed", "error");
    }
  } catch (error) {
    showAlert("loginAlert", "Connection error", "error");
  }
});

function logout() {
  authToken = null;
  document.getElementById("loginSection").style.display = "block";
  document.getElementById("dashboard").classList.remove("active");
  document.getElementById("username").value = "";
  document.getElementById("password").value = "";
  localStorage.removeItem("AauthToken");
  localStorage.removeItem("Ausername");
}

// Load Operators
async function loadOperators() {
  try {
    const response = await apiFetch(`/api/admin/operators`);
    const data = await response.json();
    const tbody = document.getElementById("operatorsBody");

    if (data.operators && data.operators.length > 0) {
      tbody.innerHTML = data.operators
        .map((op) => {
          const roleClasses =
            op.role === "admin"
              ? "inline-block bg-gray-900 text-white text-xs px-2 py-1 rounded"
              : "inline-block bg-gray-200 text-gray-800 text-xs px-2 py-1 rounded";

          return `
                        <tr>
                            <td class="px-3 py-2"><strong>${
                              op.username
                            }</strong></td>
                            <td class="px-3 py-2"><span class="${roleClasses}">${
            op.role
          }</span></td>
                            <td class="px-3 py-2 text-sm text-gray-500">${new Date(
                              op.createdAt
                            ).toLocaleDateString()}</td>
                            <td class="px-3 py-2">
                                <div class="flex gap-2">
                                    ${
                                      op.username !== "admin"
                                        ? `
                                        <button class="bg-red-600 text-white text-sm px-2 py-1 rounded" onclick="deleteOperator('${op._id}', '${op.username}')">Delete</button>
                                    `
                                        : '<span class="text-sm text-gray-400">Protected</span>'
                                    }
                                </div>
                            </td>
                        </tr>
                    `;
        })
        .join("");
    } else {
      tbody.innerHTML =
        '<tr><td colspan="4" class="text-center text-gray-500 py-8">No operators found</td></tr>';
    }
  } catch (error) {
    console.error("Error loading operators:", error);
    showAlert("operatorAlert", "Failed to load operators", "error");
  }
}

// Load Devices
async function loadDevices() {
  try {
    const response = await apiFetch(`/api/admin/devices`);
    const data = await response.json();
    const tbody = document.getElementById("devicesBody");

    if (data.devices && data.devices.length > 0) {
      tbody.innerHTML = data.devices
        .map((device) => {
          const statusClasses =
            device.status === "active"
              ? "inline-block bg-green-100 text-green-800 text-xs px-2 py-1 rounded"
              : "inline-block bg-red-100 text-red-800 text-xs px-2 py-1 rounded";

          return `
                        <tr>
                            <td class="px-3 py-2"><strong>${
                              device.fingerprint
                            }</strong></td>
                            <td class="px-3 py-2"><code class="bg-gray-100 px-2 py-1 rounded text-sm">${device.public_key.slice(
                              0,
                              30
                            )}...</code></td>
                            <td class="px-3 py-2"><span class="${statusClasses}">${
            device.status
          }</span></td>
                            <td class="px-3 py-2 text-sm text-gray-500">${new Date(
                              device.lastSeen
                            ).toLocaleString()}</td>
                            <td class="px-3 py-2">
                                <div class="flex gap-2">
                                    <button class="bg-red-600 text-white text-sm px-2 py-1 rounded" onclick="deleteDevice('${
                                      device._id
                                    }', '${
            device.fingerprint
          }')">Delete</button>
                                </div>
                            </td>
                        </tr>
                    `;
        })
        .join("");
    } else {
      tbody.innerHTML =
        '<tr><td colspan="5" class="text-center text-gray-500 py-8">No devices registered</td></tr>';
    }
  } catch (error) {
    console.error("Error loading devices:", error);
    showAlert("deviceAlert", "Failed to load devices", "error");
  }
}

// Add Operator
function openAddOperator() {
  document.getElementById("addOperatorModal").classList.add("active");
  document.getElementById("addOperatorForm").reset();
  document.getElementById("operatorModalAlert").innerHTML = "";
}

function closeAddOperator() {
  document.getElementById("addOperatorModal").classList.remove("active");
}

document
  .getElementById("addOperatorForm")
  .addEventListener("submit", async (e) => {
    e.preventDefault();

    const username = document.getElementById("operatorUsername").value;
    const password = document.getElementById("operatorPassword").value;
    const role = document.getElementById("operatorRole").value;

    try {
      const response = await apiFetch(`/api/admin/operators`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ username, password, role }),
      });

      const data = await response.json();

      if (response.ok) {
        showAlert(
          "operatorAlert",
          `Operator "${username}" added successfully`,
          "success"
        );
        closeAddOperator();
        loadOperators();
      } else {
        showAlert(
          "operatorModalAlert",
          data.error || "Failed to add operator",
          "error"
        );
      }
    } catch (error) {
      showAlert("operatorModalAlert", "Connection error", "error");
    }
  });

// Add Device
function openAddDevice() {
  document.getElementById("addDeviceModal").classList.add("active");
  document.getElementById("addDeviceForm").reset();
  document.getElementById("deviceModalAlert").innerHTML = "";
}

function closeAddDevice() {
  document.getElementById("addDeviceModal").classList.remove("active");
}

document
  .getElementById("addDeviceForm")
  .addEventListener("submit", async (e) => {
    e.preventDefault();

    const fingerprint = document.getElementById("fingerprint").value;
    const public_key = document.getElementById("devicePublicKey").value.trim();

    try {
      const response = await apiFetch(`/api/admin/devices`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ fingerprint, public_key }),
      });

      const data = await response.json();

      if (response.ok) {
        showAlert(
          "deviceAlert",
          `Device "${fingerprint}" added successfully`,
          "success"
        );
        closeAddDevice();
        loadDevices();
      } else {
        showAlert(
          "deviceModalAlert",
          data.error || "Failed to add device",
          "error"
        );
      }
    } catch (error) {
      showAlert("deviceModalAlert", "Connection error", "error");
    }
  });

// Delete Operator
async function deleteOperator(id, username) {
  if (!confirm(`Delete operator "${username}"?`)) return;

  try {
    const response = await apiFetch(`/api/admin/operators/${id}`, {
      method: "DELETE",
    });

    if (response.ok) {
      showAlert("operatorAlert", `Operator "${username}" deleted`, "success");
      loadOperators();
    } else {
      showAlert("operatorAlert", "Failed to delete operator", "error");
    }
  } catch (error) {
    showAlert("operatorAlert", "Connection error", "error");
  }
}

// Delete Device
async function deleteDevice(id, fingerprint) {
  if (!confirm(`Delete device "${fingerprint}"?`)) return;

  try {
    const response = await apiFetch(`/api/admin/devices/${id}`, {
      method: "DELETE",
    });

    if (response.ok) {
      showAlert("deviceAlert", `Device "${fingerprint}" deleted`, "success");
      loadDevices();
    } else {
      showAlert("deviceAlert", "Failed to delete device", "error");
    }
  } catch (error) {
    showAlert("deviceAlert", "Connection error", "error");
  }
}

function showAlert(elementId, message, type) {
  const alertDiv = document.getElementById(elementId);
  const classes =
    type === "success"
      ? "bg-green-50 border border-green-200 text-green-800 px-4 py-2 rounded"
      : "bg-red-50 border border-red-200 text-red-800 px-4 py-2 rounded";

  alertDiv.innerHTML = `<div class="${classes}">${message}</div>`;
  setTimeout(() => {
    alertDiv.innerHTML = "";
  }, 5000);
}

// Close modals on background click
document.querySelectorAll(".modal").forEach((modal) => {
  modal.addEventListener("click", (e) => {
    if (e.target === modal) {
      modal.classList.remove("active");
    }
  });
});
