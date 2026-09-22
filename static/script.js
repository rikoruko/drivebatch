import { FFmpeg } from '@ffmpeg/ffmpeg';
import { toBlobURL } from '@ffmpeg/util';

const ffmpeg = new FFmpeg();
const MAX_MEM_BYTES = 1073741824; // 1GB
window.currentScanItems = [];

async function initFFmpeg() {
    if (!ffmpeg.loaded) {
        await ffmpeg.load({
            coreURL: await toBlobURL('https://unpkg.com/@ffmpeg/core@0.12.6/dist/umd/ffmpeg-core.js', 'text/javascript'),
            wasmURL: await toBlobURL('https://unpkg.com/@ffmpeg/core@0.12.6/dist/umd/ffmpeg-core.wasm', 'application/wasm'),
        });
    }
}

function updateUI(msg, pct) {
    const statusEl = document.getElementById('status');
    const bar = document.getElementById('progress-bar');
    if (statusEl) statusEl.innerText = `Status: ${msg}`;
    if (bar) {
        bar.style.width = `${pct}%`;
        bar.innerText = `${pct}%`;
    }
}

async function compressVideo(item) {
    if (item.size > MAX_MEM_BYTES) {
        alert("File too large to compress in-browser. Please download the raw file directly.");
        return;
    }

    await initFFmpeg();
    updateUI("Buffering video stream...", 10);

    const resp = await fetch(item.direct_url);
    const reader = resp.body.getReader();
    const buf = new Uint8Array(item.size);
    let off = 0, lastU = 0;
    const updateInterval = 5 * 1024 * 1024; // 5MB

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf.set(value, off);
        off += value.length;

        if (off - lastU >= updateInterval || off === item.size) {
            lastU = off;
            const percent = Math.round((off / item.size) * 30);
            updateUI(`Buffering: ${Math.round(off/1e6)}MB...`, 10 + percent);
            await new Promise(r => setTimeout(r, 0));
        }
    }

    await ffmpeg.writeFile(item.name, buf);
    updateUI("Compressing via local WebAssembly...", 50);

    await ffmpeg.exec(['-i', item.name, '-vf', 'scale=-2:720', '-c:v', 'libx264', '-crf', '26', 'out.mp4']);
    
    const data = await ffmpeg.readFile('out.mp4');
    const url = URL.createObjectURL(new Blob([data.buffer]));
    const a = document.createElement('a');
    a.href = url; a.download = `compressed_${item.name}`; a.click();
    
    URL.revokeObjectURL(url);
    await ffmpeg.deleteFile(item.name); await ffmpeg.deleteFile('out.mp4');
    updateUI("Done!", 100);
}

async function downloadBatch(items) {
    for (const item of items) {
        const a = document.createElement('a');
        a.href = item.direct_url; a.download = item.name;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        await new Promise(r => setTimeout(r, 2000));
    }
}

async function downloadZip(items) {
    const zip = new JSZip();
    let processed = 0;
    for (const item of items) {
        processed++;
        updateUI(`Adding ${item.name} to ZIP ${processed}/${items.length}`, (processed / items.length) * 80);
        const res = await fetch(item.direct_url);
        zip.file(item.name, await res.blob());
        await new Promise(r => setTimeout(r, 1000));
    }
    updateUI("Assembling ZIP...", 90);
    const blob = await zip.generateAsync({type: "blob"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = "DriveBatch_Archive.zip";
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    updateUI("ZIP Download completed!", 100);
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('download-zip').addEventListener('click', () => downloadZip(window.currentScanItems));
    document.getElementById('download-individual').addEventListener('click', () => downloadBatch(window.currentScanItems));
});
