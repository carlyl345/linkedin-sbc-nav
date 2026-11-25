// Runs only when the popup asks it to (i.e. after the user clicks the toolbar button).
// It takes a screenshot of the visible tab and posts it to the local tracker.
// It never injects scripts, never reads the page's code and never sends requests to LinkedIn.

const DEFAULTS = { port: 8765, secret: "" };

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(["port", "secret"])) };
}

function localIsoNow() {
  // ISO 8601 with the browser's UTC offset, so "10:32 AM" in the screenshot resolves correctly.
  const d = new Date();
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
  const local = new Date(d.getTime() + off * 60000).toISOString().slice(0, 19);
  return `${local}${sign}${pad(off / 60)}:${pad(off % 60)}`;
}

async function capture(contactOverride) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  // activeTab (granted by the click) lets us see the tab URL; we only check it, nothing else.
  if (!tab || !/^https:\/\/www\.linkedin\.com\/messaging\//.test(tab.url || "")) {
    return { ok: false, message: "Open a LinkedIn conversation (linkedin.com/messaging) first." };
  }
  const { port, secret } = await settings();
  if (!secret) return { ok: false, message: "Add the shared secret in Settings first." };

  const image = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  let resp;
  try {
    resp = await fetch(`http://127.0.0.1:${port}/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Tracker-Secret": secret },
      body: JSON.stringify({ image, captured_at: localIsoNow(), contact: contactOverride || null }),
    });
  } catch (e) {
    return { ok: false, message: `Tracker not reachable on port ${port}. Is Claude Desktop running?` };
  }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) return { ok: false, message: body.detail || `Tracker error ${resp.status}` };
  return body;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "capture") {
    capture(msg.contact).then(sendResponse, (e) => sendResponse({ ok: false, message: String(e) }));
    return true; // async response
  }
});
