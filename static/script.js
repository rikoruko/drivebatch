// Extract references cleanly from global CDN declarations to support simple native serving structures
const { FFmpeg } = window.FFmpeg || {};
const { toBlobURL } = window.FFmpegUtil || {};

const ffmpeg = new FFmpeg();
const MAX_MEM_BYTES = 1073741824; // 1GB Memory Ceiling Cap
window.currentScanItems = []; // Populated by your main page after a successful scan

async function initFFmpeg() {
    if (!ffmpeg.loaded) {
        await ffmpeg.load({
            coreURL: 'https://unpkg.com',
            wasmURL: 'https://unpkg.com'
        });
    }
}

function updateUI(msg, pct) {
    const statusEl = document.getElementById('status');
    if (statusEl) statusEl.innerText = `Status: ${msg}`;
    
    const b = document.getElementById('progress-bar');
    if (b) {
        b.style.width = `${pct}%`; 
        b.innerText = `${Math.round(pct)}%`;
    }
}

// --- FEATURE 1: BROWSWER-BASED ZIP ASSEMBLY (0 SERVER EGRESS) ---
async function downloadZip(items) {
    if (!items || items.length === 0) {
        alert("Please paste a valid folder link and scan for assets first.");
        return;
    }
    updateUI("Initializing browser-side memory archive packager...", 5);
    
    const zip = new JSZip();
    let processedCount = 0;

    for (const item of items) {
        processedCount++;
        updateUI(`Fetching asset into archive ${processedCount}/${items.length}: ${item.name}`, (processedCount / items.length) * 80);
        
        try {
            const res = await fetch(item.direct_url);
            const dataBlob = await res.blob();
            zip.file(item.name, dataBlob);
        } catch (e) {
            console.error(`Skipped compilation for asset: ${item.name}`, e);
        }
        await new Promise(r => setTimeout(r, 1000));
    }
    
    updateUI("Assembling ZIP archive inside browser context...", 90);
    const blob = await zip.generateAsync({type: "blob"});
    
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = "DriveBatch_Archive.zip";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    updateUI("ZIP Download completed!", 100);
}

// --- FEATURE 2: NATIVE STAGGERED INDIVIDUAL FLOWS ---
async function downloadBatch(items) {
    if (!items || items.length === 0) {
        alert("Please paste a valid folder link and scan for assets first.");
        return;
    }
    updateUI("Starting staggered individual downloads...", 10);
    let count = 0;
    
    for (const item of items) {
        count++;
        const a = document.createElement('a');
        a.href = item.direct_url; a.download = item.name;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        
        const progress = (count / items.length) * 100;
        updateUI(`Triggered download ${count} of ${items.length}: ${item.name}`, progress);
        
        // Critical 2-second stagger prevents browser multi-file security blocks from popping up
        await new Promise(r => setTimeout(r, 2000));
    }
    updateUI("All downloads triggered natively!", 100);
}

// --- FEATURE 3: LOCAL HARDWARE COMPRESSION (OPTIONAL HOOK) ---
async function compressVideo(item) {
    if (item.size > MAX_MEM_BYTES) {
        alert("File too large to compress in-browser. Please download directly.");
        return;
    }
    await initFFmpeg();
    const resp = await fetch(item.direct_url);
    const reader = resp.body.getReader();
    const buf = new Uint8Array(item.size);
    let off = 0, lastU = 0;
    
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf.set(value, off);
        off += value.length;
        if (off - lastU >= 5*1024*1024 || off === item.size) {
            lastU = off;
            updateUI(`Buffering... ${Math.round(off/1e6)}MB`, 10 + Math.round(off/item.size*30));
            await new Promise(r => setTimeout(r, 0));
        }
    }
    await ffmpeg.writeFile(item.name, buf);
    updateUI("Processing file via local WebAssembly compression...", 50);
    
    await ffmpeg.exec(['-i', item.name, '-vf', 'scale=-2:720', '-c:v', 'libx264', 'out.mp4']);
    const data = await ffmpeg.readFile('out.mp4');
    const url = URL.createObjectURL(new Blob([data.buffer]));
    const a = document.createElement('a');
    a.href = url; a.download = `compressed_${item.name}`; a.click();
    URL.revokeObjectURL(url);
    await ffmpeg.deleteFile(item.name); await ffmpeg.deleteFile('out.mp4');
    updateUI("Done!", 100);
}

// --- DOM CONFIGURATION BINDINGS ---
document.addEventListener('DOMContentLoaded', () => {
    const zipBtn = document.getElementById('download-zip');
    const individualBtn = document.getElementById('download-individual');
    
    if (zipBtn) {
        zipBtn.addEventListener('click', () => downloadZip(window.currentScanItems));
    }
    if (individualBtn) {
        individualBtn.addEventListener('click', () => downloadBatch(window.currentScanItems));
    }
});
