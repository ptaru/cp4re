import dgram from 'node:dgram';
import net from 'node:net';

export const WAKE_PORT = 50162;
export const WAKE_PAYLOAD = Buffer.from('43503457414b45015003bd2f', 'hex');
export const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

export function sendWake(host: string): Promise<void> {
    return new Promise((resolve, reject) => {
        const socket = dgram.createSocket('udp4');
        const finish = (error?: Error | null) => {
            socket.close();
            error ? reject(error) : resolve();
        };
        socket.once('error', finish);
        socket.send(WAKE_PAYLOAD, WAKE_PORT, host, error => {
            socket.removeListener('error', finish);
            finish(error);
        });
    });
}

// TCP only: probing /live.ps would count as a viewer and change sleep state.
export function probe(host: string, port: number, timeout: number): Promise<boolean> {
    return new Promise(resolve => {
        const socket = net.createConnection({ host, port });
        let done = false;
        const finish = (ready: boolean) => {
            if (done) return;
            done = true;
            socket.destroy();
            resolve(ready);
        };
        socket.setTimeout(timeout);
        socket.once('connect', () => finish(true));
        socket.once('error', () => finish(false));
        socket.once('timeout', () => finish(false));
    });
}

export interface WakeIO {
    probe: typeof probe;
    sendWake: typeof sendWake;
    now: () => number;
    sleep: typeof delay;
}
const defaultIO: WakeIO = { probe, sendWake, now: Date.now, sleep: delay };

export async function waitForCamera(host: string, port: number, timeoutMs: number,
    log: (message: string) => void, io = defaultIO): Promise<void> {
    const deadline = io.now() + timeoutMs;
    let lastWake = -Infinity;
    while (io.now() < deadline) {
        if (await io.probe(host, port, Math.min(750, deadline - io.now()))) return;
        if (io.now() >= deadline) break;
        if (io.now() - lastWake >= 5000) {
            log(`Sending CP4 wake to ${host}:${WAKE_PORT}`);
            await io.sendWake(host);
            lastWake = io.now();
        }
        await io.sleep(Math.min(250, Math.max(0, deadline - io.now())));
    }
    throw new Error(`CP4 video server ${host}:${port} did not respond within ${timeoutMs / 1000}s. Check LAN routing, SD hook, and patched wake receiver.`);
}

export class WakeGate {
    private pending?: Promise<void>;
    ensure(work: () => Promise<void>): Promise<void> {
        if (!this.pending) {
            this.pending = work().finally(() => { this.pending = undefined; });
        }
        return this.pending;
    }
}
