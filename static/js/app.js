(() => {
  "use strict";

  const root = document.getElementById("uploader");
  if (!root) return;

  const $ = (id) => document.getElementById(id);
  const MAX_BYTES = Number(root.dataset.maxBytes);
  const MAX_MIB = root.dataset.maxMib;

  const panels = {
    empty: root.querySelector('[data-panel="empty"]'),
    selected: root.querySelector('[data-panel="selected"]'),
    success: root.querySelector('[data-panel="success"]'),
  };
  const input = $("file-input");
  const dropzone = $("dropzone");
  const form = $("upload-form");
  const uploadBtn = $("upload-btn");
  const cancelBtn = $("cancel-btn");
  const clearBtn = $("clear-btn");

  let selectedFile = null;
  let xhr = null;
  let uploaded = null; // { id, deleteToken }

  // ------------------------------------------------------------ formatting
  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KiB", "MiB", "GiB"];
    let value = bytes / 1024;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`;
  }

  function extensionLabel(name) {
    const m = /\.([A-Za-z0-9]{1,5})$/.exec(name);
    return m ? m[1].toUpperCase() : "FILE";
  }

  function formatDate(iso) {
    try {
      return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
    } catch { return iso; }
  }

  // ------------------------------------------------------------ state
  function setState(state) {
    root.dataset.state = state;
    panels.empty.hidden = state !== "empty";
    panels.selected.hidden = !["selected", "uploading", "error"].includes(state);
    panels.success.hidden = state !== "success";

    const uploading = state === "uploading";
    $("progress-wrap").hidden = !uploading;
    cancelBtn.hidden = !uploading;
    clearBtn.hidden = uploading;
    uploadBtn.disabled = uploading;
    uploadBtn.setAttribute("aria-busy", String(uploading));
    $("upload-btn-label").textContent = uploading ? "Uploading…" : state === "error" ? "Try again" : "Upload file";
    if (state !== "error") $("error-box").hidden = true;
  }

  function showError(message) {
    $("error-text").textContent = message;
    $("error-box").hidden = false;
    setState("error");
  }

  function reset() {
    if (xhr) xhr.abort();
    selectedFile = null;
    uploaded = null;
    input.value = "";
    setProgress(0);
    setState("empty");
    input.focus();
  }

  function setProgress(pct) {
    const v = Math.max(0, Math.min(100, Math.round(pct)));
    $("progress-fill").style.width = `${v}%`;
    $("progress-pct").textContent = `${v}%`;
    $("progress-bar").setAttribute("aria-valuenow", String(v));
  }

  // ------------------------------------------------------------ selection
  function selectFile(file) {
    if (!file) return;
    selectedFile = file;
    $("file-name").textContent = file.name;
    $("file-name").title = file.name;
    $("file-size").textContent = formatBytes(file.size);
    $("file-type").textContent = file.type || "Unknown type";
    $("file-ext").textContent = extensionLabel(file.name);
    setState("selected");

    // Client-side checks are a convenience; the server enforces the same rules.
    if (file.size === 0) {
      showError("That file is empty. Choose a different file.");
    } else if (file.size > MAX_BYTES) {
      showError(`This file is ${formatBytes(file.size)}. The limit is ${MAX_MIB} MiB. Choose a smaller file.`);
      uploadBtn.disabled = true;
      $("upload-btn-label").textContent = "Upload file";
    } else {
      uploadBtn.focus();
    }
  }

  input.addEventListener("change", () => selectFile(input.files[0]));
  clearBtn.addEventListener("click", reset);

  // Drag and drop on the whole panel (more forgiving target than the label).
  let dragDepth = 0;
  const isFileDrag = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
  root.addEventListener("dragenter", (e) => {
    if (!isFileDrag(e) || root.dataset.state !== "empty") return;
    e.preventDefault();
    dragDepth++;
    dropzone.classList.add("is-dragging");
  });
  root.addEventListener("dragover", (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = root.dataset.state === "empty" ? "copy" : "none";
  });
  root.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropzone.classList.remove("is-dragging");
  });
  root.addEventListener("drop", (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    dragDepth = 0;
    dropzone.classList.remove("is-dragging");
    if (root.dataset.state !== "empty") return;
    // One file per upload; if several are dropped, the first one is used.
    selectFile(e.dataTransfer.files[0]);
  });
  // Stop the browser from opening files dropped outside the panel.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  // ------------------------------------------------------------ upload
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!selectedFile || root.dataset.state === "uploading") return;
    if (selectedFile.size > MAX_BYTES || selectedFile.size === 0) return;

    const body = new FormData();
    body.append("file", selectedFile, selectedFile.name);

    xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.setRequestHeader("X-Requested-With", "fetch");
    xhr.responseType = "json";
    xhr.timeout = 5 * 60 * 1000;

    xhr.upload.addEventListener("progress", (ev) => {
      if (ev.lengthComputable) setProgress((ev.loaded / ev.total) * 100);
    });
    xhr.upload.addEventListener("load", () => {
      setProgress(100);
      $("progress-label").textContent = "Finishing up…";
    });

    xhr.addEventListener("load", () => {
      const res = xhr.response || {};
      const status = xhr.status;
      xhr = null;
      if (res.ok) return showSuccess(res);
      showError(res.error?.message || defaultMessage(status));
    });
    xhr.addEventListener("error", () => { xhr = null; showError("The connection was interrupted. Check your network and try again."); });
    xhr.addEventListener("timeout", () => { xhr = null; showError("The upload took too long. Please try again."); });
    xhr.addEventListener("abort", () => { xhr = null; });

    setProgress(0);
    $("progress-label").textContent = "Uploading…";
    setState("uploading");
    xhr.send(body);
  });

  function defaultMessage(status) {
    if (status === 413) return `Files can be up to ${MAX_MIB} MiB.`;
    if (status === 429) return "Too many uploads. Please wait a moment and try again.";
    return "The upload failed. Please try again.";
  }

  cancelBtn.addEventListener("click", () => {
    if (xhr) xhr.abort();
    setProgress(0);
    setState("selected");
    uploadBtn.focus();
  });

  // ------------------------------------------------------------ success
  function showSuccess(res) {
    uploaded = { id: res.file.id, deleteToken: res.delete_token };
    $("share-link").value = res.download_url;
    $("delete-key").value = res.delete_token;
    $("delete-status").textContent = "";
    $("delete-btn").disabled = false;
    $("success-meta").textContent = `${res.file.name} · ${formatBytes(res.file.size)}`;
    $("expiry-note").textContent = res.file.expires_at
      ? `The link works until ${formatDate(res.file.expires_at)}, when the file is deleted automatically. Anyone with the link can download it.`
      : "The link keeps working until the file is deleted. Anyone with the link can download it.";
    setCopyLabel("Copy link");
    setState("success");
    $("success-heading").focus();
  }

  function setCopyLabel(text) { $("copy-label").textContent = text; }

  $("copy-btn").addEventListener("click", async () => {
    const link = $("share-link");
    let copied = false;
    try {
      await navigator.clipboard.writeText(link.value);
      copied = true;
    } catch {
      link.select();
      try { copied = document.execCommand("copy"); } catch { copied = false; }
    }
    setCopyLabel(copied ? "Copied" : "Press Ctrl+C");
    $("copy-status").textContent = copied ? "Link copied to clipboard." : "Copy failed. The link is selected; copy it manually.";
    if (!copied) link.select();
    clearTimeout(setCopyLabel.timer);
    setCopyLabel.timer = setTimeout(() => setCopyLabel("Copy link"), 2200);
  });

  $("share-link").addEventListener("focus", (e) => e.target.select());
  $("delete-key").addEventListener("focus", (e) => e.target.select());

  $("delete-btn").addEventListener("click", async () => {
    if (!uploaded) return;
    if (!window.confirm("Delete this file? The link will stop working.")) return;
    const btn = $("delete-btn");
    const status = $("delete-status");
    btn.disabled = true;
    try {
      const resp = await fetch(`/api/files/${encodeURIComponent(uploaded.id)}`, {
        method: "DELETE",
        headers: { "X-Requested-With": "fetch", "X-Delete-Token": uploaded.deleteToken },
      });
      const data = await resp.json().catch(() => ({}));
      if (!data.ok) throw new Error(data.error?.message || "The file could not be deleted.");
      status.className = "mt-2 text-sm text-emerald-700 dark:text-emerald-300";
      status.textContent = "Deleted. The link no longer works.";
      $("share-link").value = "";
      $("copy-btn").disabled = true;
    } catch (err) {
      btn.disabled = false;
      status.className = "mt-2 text-sm text-red-700 dark:text-red-300";
      status.textContent = err.message;
    }
  });

  $("again-btn").addEventListener("click", () => {
    $("copy-btn").disabled = false;
    reset();
  });

  if (root.dataset.state !== "disabled") setState("empty");
})();
