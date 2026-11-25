const $ = (id) => document.getElementById(id);

function show(text, ok) {
  $("status").textContent = text;
  $("status").className = ok ? "ok" : "err";
}

chrome.storage.local.get(["secret", "port"]).then(({ secret, port }) => {
  if (secret) $("secret").value = secret;
  if (port) $("port").value = port;
  if (!secret) $("settings").open = true;
});

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ secret: $("secret").value.trim(), port: Number($("port").value) || 8765 });
  show("Settings saved.", true);
});

$("capture").addEventListener("click", async () => {
  $("capture").disabled = true;
  show("Reading conversation…", true);
  const res = await chrome.runtime.sendMessage({ type: "capture", contact: $("contact").value.trim() });
  $("capture").disabled = false;
  show(res?.message || "No response from tracker.", !!res?.ok);
  if (res?.ok) $("contact").value = "";
});
