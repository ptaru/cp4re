import http from 'node:http';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { AddressInfo } from 'node:net';

// Delay the MP4 header until AAC's bitstream filter has produced codec config.
// RTSP writes SDP before its first packet, so applying the filter there is too late.
export const remuxArguments = (url: string) => [
    '-hide_banner', '-loglevel', 'warning', '-rw_timeout', '15000000',
    '-f', 'mpeg', '-i', url, '-map', '0:v:0', '-map', '0:a:0',
    '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-f', 'mp4',
    '-movflags', 'frag_keyframe+delay_moov+default_base_moof',
    '-frag_duration', '200000', '-flush_packets', '1', 'pipe:1',
];

export async function createBridge(ffmpeg: string, log: (message: string) => void) {
    const token = randomBytes(24).toString('hex');
    const sources = new Map<string, string>();
    const server = http.createServer((req, res) => {
        const source = sources.get(req.url || '');
        if (!source || req.method !== 'GET') { res.writeHead(404).end(); return; }
        const child = spawn(ffmpeg, remuxArguments(source), { stdio: ['ignore', 'pipe', 'pipe'] });
        res.setHeader('Content-Type', 'video/mp4');
        child.stdout.pipe(res);
        child.stderr.on('data', data => log(String(data).trim()));
        const cleanup = () => {
            child.kill('SIGTERM');
            const timer = setTimeout(() => child.kill('SIGKILL'), 2000);
            timer.unref();
            child.once('close', () => clearTimeout(timer));
        };
        res.once('close', cleanup);
        child.once('error', error => { log(error.message); res.destroy(error); });
        child.once('close', () => res.end());
    });
    await new Promise<void>((resolve, reject) => {
        server.once('error', reject);
        server.listen(0, '127.0.0.1', () => { server.removeListener('error', reject); resolve(); });
    });
    const port = (server.address() as AddressInfo).port;
    return {
        url(source: string) {
            // Stable per source; no unbounded per-request route allocation.
            const path = `/${token}/${encodeURIComponent(source)}`;
            sources.set(path, source);
            return `http://127.0.0.1:${port}${path}`;
        },
    };
}
