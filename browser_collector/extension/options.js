const endpoint = document.getElementById("endpoint");
const token = document.getElementById("token");
const status = document.getElementById("status");

chrome.storage.local.get({ endpoint: "", token: "" }).then((data) => {
  endpoint.value = data.endpoint || "";
  token.value = data.token || "";
});

document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    endpoint: endpoint.value.trim().replace(/\/$/, ""),
    token: token.value.trim()
  });
  status.textContent = "Saved.";
});
