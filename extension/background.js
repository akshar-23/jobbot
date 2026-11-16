// jobbot helper: background service worker.
// Injects content.js when you click the toolbar button (or press Alt+J), and relays the
// content script's requests to the local jobbot server. Pages can't call http://127.0.0.1
// themselves (mixed content), so every request goes through here.

const SERVER = "http://127.0.0.1:8765";

async function inject(tab) {
  if (!tab || !tab.id || !/^https?:/.test(tab.url || "")) return;
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
}

chrome.action.onClicked.addListener(inject);
chrome.commands.onCommand.addListener(async (command, tab) => {
  if (command !== "fill-page") return;
  const target = tab || (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
  inject(target);
});

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

async function call(path, body) {
  // X-Jobbot marks requests as coming from jobbot's own UI; the server refuses anything without it.
  const options = body === undefined
    ? { headers: { "X-Jobbot": "1" } }
    : { method: "POST", headers: { "X-Jobbot": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(SERVER + path, options);
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try { message = (await res.json()).error || message; } catch (_) { /* not JSON */ }
    throw new Error(message);
  }
  if ((res.headers.get("content-type") || "").includes("pdf")) {
    return { pdf: toBase64(await res.arrayBuffer()) };
  }
  return res.json();
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  call(message.path, message.body)
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) => sendResponse({ ok: false, error: String(error && error.message || error) }));
  return true; // keep the channel open for the async reply
});
