// Exercise the actual webpack artifact with the current host's CJS SDK binding.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

for (const binding of ['cjs', 'legacy', 'globals']) {
test(`bundled camera initializes with ${binding} runtime bindings`, async () => {
    const values = new Map();
    const runtime = {
        deviceManager: {
            getDeviceLogger: () => ({}),
            getDeviceState: () => ({ id: 'cp4-test' }),
            getDeviceStorage: () => ({ getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) }),
            onDeviceEvent: async () => {},
        },
        systemManager: {},
        endpointManager: {}, mediaManager: {}, pluginHostAPI: {},
    };
    const errors = [];
    const context = {
        exports: {},
        process: { env: binding === 'cjs' ? { SCRYPTED_SDK_CJS_MODULE: '/mock/runtime.cjs' }
            : binding === 'legacy' ? { SCRYPTED_SDK_MODULE: '/mock/runtime.cjs' } : {} },
        ...(binding === 'globals' ? runtime : {}),
        require: name => name === '/mock/runtime.cjs'
            ? { getScryptedStatic: () => runtime } : require(name),
        console: { ...console, warn: () => {}, error: (...args) => errors.push(args) },
        Buffer, setTimeout, clearTimeout,
    };
    vm.runInNewContext(fs.readFileSync('out/main.nodejs.js', 'utf8'), context);
    assert.deepEqual(errors, []);
    const camera = new context.exports.default();
    assert.equal(camera.id, 'cp4-test');
    await camera.putSetting('host', '192.0.2.1');
    assert.equal((await camera.getSettings())[0].value, '192.0.2.1');
    assert.deepEqual(errors, []);
});

}
