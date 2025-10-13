const http = require("http");

const server = http.createServer((req, res) => {
  if (req.method === "GET") {
    console.log("GET request received");
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("Hello World");
  }
});

server.listen(8084, '0.0.0.0', () => {
    console.log("Server running on port 8084");
});
