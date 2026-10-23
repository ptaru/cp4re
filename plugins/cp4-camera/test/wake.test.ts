import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WAKE_PAYLOAD, WAKE_PORT, waitForCamera, WakeGate, WakeIO } from '../src/wake';

function fake(readyAt: number) {
    let now = 0;
    const sends: number[] = [];
    const io: WakeIO = {
        now: () => now,
        sleep: async ms => { now += ms; },
        probe: async () => now >= readyAt,
        sendWake: async () => { sends.push(now); },
    };
    return { io, sends };
}
test('exact patched receiver packet', () => {
    assert.equal(WAKE_PORT, 50162);
    assert.equal(WAKE_PAYLOAD.toString('hex'), '43503457414b45015003bd2f');
});
test('awake camera receives no wake packets', async () => {
    const { io, sends } = fake(0);
    await waitForCamera('192.0.2.1', 8080, 10000, () => {}, io);
    assert.deepEqual(sends, []);
});
test('sleeping camera wakes with five second retry spacing', async () => {
    const { io, sends } = fake(6500);
    await waitForCamera('192.0.2.1', 8080, 10000, () => {}, io);
    assert.deepEqual(sends, [0, 5000]);
});
test('unreachable camera fails within configured timeout', async () => {
    const { io, sends } = fake(Infinity);
    await assert.rejects(waitForCamera('192.0.2.1', 8080, 6000, () => {}, io), /within 6s/);
    assert.equal(io.now(), 6000);
    assert.deepEqual(sends, [0, 5000]);
});
test('concurrent requests share wake and failed attempts can retry', async () => {
    const gate = new WakeGate();
    let calls = 0;
    let release!: () => void;
    const work = () => { calls++; return new Promise<void>(resolve => { release = resolve; }); };
    const first = gate.ensure(work);
    assert.equal(gate.ensure(work), first);
    release();
    await first;
    await assert.rejects(gate.ensure(async () => { calls++; throw Error('offline'); }));
    await gate.ensure(async () => { calls++; });
    assert.equal(calls, 3);
});

test('TCP probe detects a real listener and a closed port', async () => {
    const net = await import('node:net');
    const { probe } = await import('../src/wake');
    const server = net.createServer(socket => socket.end());
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    const port = (server.address() as import('node:net').AddressInfo).port;
    assert.equal(await probe('127.0.0.1', port, 500), true);
    await new Promise<void>(resolve => server.close(() => resolve()));
    assert.equal(await probe('127.0.0.1', port, 500), false);
});
