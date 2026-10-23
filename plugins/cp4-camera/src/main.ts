import sdk, { FFmpegInput, MediaObject, RequestMediaStreamOptions, ResponseMediaStreamOptions,
    ScryptedDeviceBase, ScryptedInterface, Setting, Settings, VideoCamera } from '@scrypted/sdk';
import { createBridge } from './bridge';
import { isIPv4 } from 'node:net';
import { waitForCamera, WakeGate } from './wake';

export default class CP4Camera extends ScryptedDeviceBase implements VideoCamera, Settings {
    private bridge?: ReturnType<typeof createBridge>;
    private gates = new Map<string, WakeGate>();

    private config() {
        const host = this.storage.getItem('host') || '';
        if (!isIPv4(host)) throw new Error('Set the camera LAN IPv4 address in Settings first.');
        return { host, port: Number(this.storage.getItem('port') || 8080),
            timeout: Number(this.storage.getItem('timeout') || 60) };
    }

    async getSettings(): Promise<Setting[]> {
        return [
            { key: 'host', title: 'Camera IPv4 address', value: this.storage.getItem('host') || '',
                description: 'Camera running the patched CP4 UDP receiver and SD video boot hook.' },
            { key: 'port', title: 'Video HTTP port', type: 'number', value: this.storage.getItem('port') || '8080' },
            { key: 'timeout', title: 'Wake timeout (seconds)', type: 'number', value: this.storage.getItem('timeout') || '60' },
        ];
    }

    async putSetting(key: string, value: string | number | boolean): Promise<void> {
        const text = String(value).trim();
        if (key === 'host') {
            if (!isIPv4(text)) throw new Error('Enter a valid IPv4 address.');
        } else if (key === 'port' || key === 'timeout') {
            const n = Number(text);
            const max = key === 'port' ? 65535 : 180;
            if (!Number.isInteger(n) || n < 1 || n > max) throw new Error(`${key} must be 1..${max}`);
        } else throw new Error('Unknown setting');
        this.storage.setItem(key, text);
        this.onDeviceEvent(ScryptedInterface.Settings, undefined);
    }

    async getVideoStreamOptions(): Promise<ResponseMediaStreamOptions[]> {
        return [{ id: 'main', name: 'CP4 HEVC + AAC', container: 'mp4', tool: 'ffmpeg',
            video: { codec: 'h265', width: 1728, height: 1080 },
            audio: { codec: 'aac', sampleRate: 16000 } }];
    }

    async getVideoStream(options?: RequestMediaStreamOptions): Promise<MediaObject> {
        if (options?.id && options.id !== 'main') throw new Error('Unknown stream');
        const { host, port, timeout } = this.config();
        const key = `${host}:${port}:${timeout}`;
        let gate = this.gates.get(key);
        if (!gate) { gate = new WakeGate(); this.gates.set(key, gate); }
        await gate.ensure(() => waitForCamera(host, port, timeout * 1000, message => this.console.log(message)));
        this.bridge ??= sdk.mediaManager.getFFmpegPath().then(path =>
            createBridge(path, message => this.console.log(message))).catch(error => {
                this.bridge = undefined; throw error;
            });
        const url = (await this.bridge).url(`http://${host}:${port}/live.ps`);
        const input: FFmpegInput = {
            url, container: 'mp4',
            inputArguments: ['-rw_timeout', '15000000', '-i', url],
            mediaStreamOptions: (await this.getVideoStreamOptions())[0],
        };
        return sdk.mediaManager.createFFmpegMediaObject(input, { sourceId: this.id });
    }
}
