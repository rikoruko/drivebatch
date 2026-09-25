/* -------------------------------------------------------------------
   DriveBatch Client-Side Script
   Zero-Egress Google Drive Stream Extractor & Batch Downloader
------------------------------------------------------------------- */

const $ = id => document.getElementById(id);

let videos = [];
let selected = new Set();
let currentVideo = null;
let currentVideoId = null;
let compressIsBatch = false;
let mediaType = "video";

const driveUrl = $("driveUrl");
const results = $("results");
const videoList = $("videoList");
const downloadBar = $("downloadBar");

/* -------------------------
   UTILITIES
------------------------- */
function bytes(n) {
  n = Number(n || 0);
  if (!n) return "0 B";

  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return n.toFixed(i ? 1 : 0) + " " + units[i];
}

function validDrive(url) {
  return /^https?:\/\/(?:www\.)?(?:drive\.google\.com|docs\.google\.com)\//i.test(url);
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  }[c]));
}

async function readJson(response) {
  const text = await response.text();
  if (!text) {
    throw new Error(`The server returned an empty response. HTTP ${response.status}`);
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    console.error("Server response:", text);
    throw new Error(`Server returned an unexpected response. HTTP ${response.status}`);
  }
}

/* -------------------------
   SELECTION MANAGEMENT
------------------------- */
function updateSelection() {
  let size = 0;
  videos.forEach(video => {
    if (selected.has(String(video.id))) {
      size += Number(video.size || 0);
    }
  });

  const itemLabel = mediaType === "image" ? "image" : mediaType === "audio" ? "track" : "video";
  $("selectedCount").textContent = `${selected.size} selected ${itemLabel}${selected.size === 1 ? "" : "s"}`;
  $("selectedSize").textContent = bytes(size);
  downloadBar.classList.toggle("hidden", selected.size === 0);
}

/* -------------------------
   RENDER LIST
------------------------- */
function renderVideos() {
  const q = $("search").value.trim().toLowerCase();
  const filtered = videos.filter(video => String(video.name || "").toLowerCase().includes(q));
  const itemLabel = mediaType === "image" ? "image" : mediaType === "audio" ? "track" : "video";

  if (!filtered.length) {
    videoList.innerHTML = `<div class="card"><p>No ${itemLabel}s found.</p></div>`;
    updateSelection();
    return;
  }

  videoList.innerHTML = filtered.map(video => {
    const id = String(video.id);
    return `
      <article class="video-card">
        <input class="video-check" type="checkbox" ${selected.has(id) ? "checked" : ""} onchange="toggleVideo('${escapeHtml(id)}')">
        <button class="preview-button" type="button" aria-label="Preview ${escapeHtml(video.name)}" onclick="openPreview('${escapeHtml(id)}')">
          <span aria-hidden="true">▶</span>
        </button>
        <div class="video-info">
          <h3>${escapeHtml(video.name)}</h3>
          <p>${escapeHtml(video.path || "Google Drive")}</p>
          <small>${bytes(video.size)}</small>
        </div>
        ${mediaType === "video" ? `
          <button class="stream-saver-button" onclick="openStreamSaver('${escapeHtml(id)}')">
            ⚡ Stream Saver
          </button>
        ` : ""}
        <button class="download-button" onclick="downloadSingleDirect('${escapeHtml(id)}')">
          ↓
        </button>
      </article>
    `;
  }).join("");

  updateSelection();
}

function toggleVideo(id) {
  id = String(id);
  if (selected.has(id)) {
    selected.delete(id);
  } else {
    selected.add(id);
  }
  renderVideos();
}

/* -------------------------
   SCAN FOLDER
------------------------- */
async function scan() {
  const url = driveUrl.value.trim();
  if (!validDrive(url)) {
    alert("Please paste a valid Google Drive folder link.");
    return;
  }

  $("scanBtn").disabled = true;
  $("scanBtn").textContent = "Scanning...";

  try {
    const response = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url, media_type: mediaType })
    });

    const data = await readJson(response);
    if (!response.ok) {
      throw new Error(data.error || "Scan failed.");
    }

    videos = data.videos || data.images || data.audio || [];
    selected.clear();

    const total = videos.reduce((sum, video) => sum + Number(video.size || 0), 0);
    const label = mediaType === "image" ? "images" : mediaType === "audio" ? "tracks" : "videos";

    $("resultsTitle").textContent = mediaType === "image" ? "Your images" : mediaType === "audio" ? "Your music" : "Your videos";
    $("folderStats").textContent = `${videos.length} ${label} • ${bytes(total)}`;

    results.classList.remove("hidden");
    renderVideos();
    results.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    alert(error.message || "Scan failed.");
  }

  $("scanBtn").disabled = false;
  $("scanBtn").textContent = "Scan Folder";
}

/* -------------------------
   PREVIEW
------------------------- */
function openPreview(id) {
  const video = videos.find(v => String(v.id) === String(id));
  if (!video) return;

  currentVideo = video;
  currentVideoId = id;

  $("previewTitle").textContent = video.name || (mediaType === "image" ? "Image" : "Video");
  const previewUrl = video.direct_url || `/api/preview/${encodeURIComponent(id)}`;

  const previewVideo = $("previewVideo");
  const previewAudio = $("previewAudio");
  const previewImage = $("previewImage");
  const isImage = mediaType === "image";
  const isAudio = mediaType === "audio";

  previewVideo.classList.toggle("hidden", isImage || isAudio);
  previewAudio.classList.toggle("hidden", !isAudio);
  previewImage.classList.toggle("hidden", !isImage);

  if (isImage) {
    previewVideo.pause(); previewVideo.removeAttribute("src"); previewVideo.load();
    previewAudio.pause(); previewAudio.removeAttribute("src"); previewAudio.load();
    previewImage.src = previewUrl; previewImage.alt = video.name || "Image";
  } else if (isAudio) {
    previewVideo.pause(); previewVideo.removeAttribute("src"); previewVideo.load();
    previewImage.removeAttribute("src");
    previewAudio.src = previewUrl;
  } else {
    previewAudio.pause(); previewAudio.removeAttribute("src"); previewAudio.load();
    previewImage.removeAttribute("src");
    previewVideo.src = previewUrl;
  }

  $("previewModal").classList.remove("hidden");
}

function closePreview() {
  const player = $("previewVideo");
  player.pause(); player.removeAttribute("src"); player.load();

  const audio = $("previewAudio");
  audio.pause(); audio.removeAttribute("src"); audio.load();

  $("previewImage").removeAttribute("src");
  $("previewModal").classList.add("hidden");
}

/* -------------------------
   SINGLE DOWNLOAD (ZERO-EGRESS)
------------------------- */
function downloadSingleDirect(id) {
  const video = videos.find(v => String(v.id) === String(id));
  if (!video) return;

  const a = document.createElement('a');
  a.href = video.direct_url;
  a.download = video.name || "download";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

/* -------------------------
   STREAM SAVER (GOOGLE PRE-PROCESSED STREAMS)
------------------------- */
function openStreamSaver(id) {
  const video = videos.find(v => String(v.id) === String(id));
  if (!video) return;

  currentVideo = video;
  currentVideoId = id;
  compressIsBatch = false;

  $("streamSaverModal").classList.remove("hidden");
}

function closeStreamSaver() {
  $("streamSaverModal").classList.add("hidden");
}

async function startCompression(quality) {
  closeStreamSaver();
  $("compressProgressModal").classList.remove("hidden");
  $("compressProgressText").textContent = "Fetching Google Drive pre-processed video stream...";
  $("compressProgressFill").style.width = "10%";

  const targetFiles = compressIsBatch
    ? videos.filter(v => selected.has(String(v.id)))
    : [currentVideo];

  if (!targetFiles.length || !targetFiles[0]) {
    alert("No video selected.");
    $("compressProgressModal").classList.add("hidden");
    return;
  }

  const zip = new JSZip();

  try {
    for (let i = 0; i < targetFiles.length; i++) {
      const file = targetFiles[i];
      const progressPercent = Math.round(((i + 1) / targetFiles.length) * 80);

      $("compressProgressText").textContent = `Pulling pre-processed stream ${i + 1} of ${targetFiles.length}: ${file.name}`;
      $("compressProgressFill").style.width = `${progressPercent}%`;

      // Updated: Pass quality parameter correctly matching Flask backend route (/api/download/<file_id>?quality=...)
      const streamUrl = `${file.direct_url}?quality=${quality}`;
      let blob;
      
      try {
        const response = await fetch(streamUrl);
        if (!response.ok) throw new Error("Stream quality variant fallback");
        blob = await response.blob();
      } catch (err) {
        // Fallback to original direct binary stream if quality endpoint fails
        const fallbackRes = await fetch(file.direct_url);
        blob = await fallbackRes.blob();
      }

      const baseName = (file.name || `video_${file.id}`).replace(/\.[^/.]+$/, "");
      const outputFilename = `${baseName}_${quality}.mp4`;

      if (targetFiles.length === 1) {
        saveAs(blob, outputFilename);
        $("compressProgressModal").classList.add("hidden");
        return;
      }

      zip.file(outputFilename, blob);
    }

    $("compressProgressText").textContent = "Creating ZIP package...";
    $("compressProgressFill").style.width = "95%";
    const zipBlob = await zip.generateAsync({ type: "blob" });
    saveAs(zipBlob, `DriveBatch_${quality}.zip`);

    $("compressProgressModal").classList.add("hidden");
  } catch (error) {
    alert("Could not fetch Google pre-processed stream: " + error.message);
    $("compressProgressModal").classList.add("hidden");
  }
}

/* -------------------------
   CLIENT-SIDE ZIP ARCHIVING
------------------------- */
async function startZipDownload() {
  $("downloadModal").classList.add("hidden");
  $("progressModal").classList.remove("hidden");
  $("progressText").textContent = "Preparing ZIP download...";
  $("progressFill").style.width = "5%";

  const targetFiles = videos.filter(v => selected.has(String(v.id)));
  if (!targetFiles.length) {
    alert("No files selected.");
    $("progressModal").classList.add("hidden");
    return;
  }

  const zip = new JSZip();

  try {
    for (let i = 0; i < targetFiles.length; i++) {
      const item = targetFiles[i];
      const percent = Math.round(((i + 1) / targetFiles.length) * 85);
      
      $("progressText").textContent = `Adding ${i + 1}/${targetFiles.length}: ${item.name}`;
      $("progressFill").style.width = `${percent}%`;

      const res = await fetch(item.direct_url);
      const blob = await res.blob();
      zip.file(item.name || `file_${item.id}`, blob);
    }

    $("progressText").textContent = "Assembling ZIP archive...";
    $("progressFill").style.width = "95%";
    
    const zipBlob = await zip.generateAsync({ type: "blob" });
    saveAs(zipBlob, "DriveBatch_Archive.zip");

    $("progressModal").classList.add("hidden");
  } catch (error) {
    alert("ZIP creation failed: " + error.message);
    $("progressModal").classList.add("hidden");
  }
}

/* -------------------------
   EVENT LISTENERS & BINDINGS
------------------------- */
window.addEventListener('DOMContentLoaded', () => {
  $("scanBtn").onclick = scan;
  driveUrl.addEventListener("keydown", event => { if (event.key === "Enter") scan(); });
  $("search").oninput = renderVideos;

  $("selectAll").onclick = () => {
    videos.forEach(video => selected.add(String(video.id)));
    renderVideos();
  };

  $("clearAll").onclick = () => {
    selected.clear();
    renderVideos();
  };

  $("closePreview").onclick = closePreview;
  $("previewDownload").onclick = () => {
    if (currentVideoId) downloadSingleDirect(currentVideoId);
  };

  $("downloadSelected").onclick = () => {
    if (!selected.size) return;
    const label = mediaType === "image" ? "image" : "video";
    $("downloadInfo").textContent = `${selected.size} ${label}${selected.size === 1 ? "" : "s"} selected.`;
    $("downloadModal").classList.remove("hidden");
  };

  $("closeDownload").onclick = () => $("downloadModal").classList.add("hidden");

  function applyMediaType(nextType) {
    mediaType = ["video", "image", "audio"].includes(nextType) ? nextType : "video";
    document.querySelectorAll(".media-toggle-btn").forEach(button => {
      button.classList.toggle("active", button.dataset.mediaType === mediaType);
    });

    $("search").value = "";
    videos = [];
    selected.clear();
    results.classList.add("hidden");
    downloadBar.classList.add("hidden");
    $("batchStreamSaver").classList.toggle("hidden", mediaType !== "video");
  }

  document.querySelectorAll(".media-toggle-btn").forEach(button => {
    button.onclick = () => applyMediaType(button.dataset.mediaType || "video");
  });

  $("individualBtn").onclick = async () => {
    $("downloadModal").classList.add("hidden");
    const ids = [...selected];
    for (const id of ids) {
      downloadSingleDirect(id);
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
  };

  $("zipBtn").onclick = startZipDownload;

  $("batchStreamSaver").onclick = () => {
    if (!selected.size || mediaType !== "video") return;
    compressIsBatch = true;
    $("streamSaverModal").classList.remove("hidden");
  };

  document.querySelectorAll(".quality-btn").forEach(button => {
    button.onclick = () => startCompression(button.getAttribute("data-quality"));
  });

  $("closeStreamSaver").onclick = closeStreamSaver;

  $("compressCancelBtn").onclick = () => {
    $("compressProgressModal").classList.add("hidden");
  };

  $("cancelBtn").onclick = () => {
    $("progressModal").classList.add("hidden");
  };

  $("menuBtn").onclick = () => $("menu").classList.toggle("open");

  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      closePreview();
      $("downloadModal").classList.add("hidden");
      $("successModal").classList.add("hidden");
      closeStreamSaver();
    }
  });

  $("previewModal").addEventListener("click", event => { if (event.target === $("previewModal")) closePreview(); });
  $("downloadModal").addEventListener("click", event => { if (event.target === $("downloadModal")) $("downloadModal").classList.add("hidden"); });
  $("successModal").addEventListener("click", event => { if (event.target === $("successModal")) $("successModal").classList.add("hidden"); });
  $("streamSaverModal").addEventListener("click", event => { if (event.target === $("streamSaverModal")) closeStreamSaver(); });
});
