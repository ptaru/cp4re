const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
test('delayed MP4 header makes ADTS AAC copyable to RTP', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cp4-aac-'));
    const run = args => {
        const result = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', ...args], { encoding: 'utf8', timeout: 15000 });
        assert.equal(result.status, 0, result.stderr || String(result.error));
    };
    try {
        run(['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '1', '-c:a', 'aac', '-f', 'adts', `${dir}/input.aac`]);
        run(['-i', `${dir}/input.aac`, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-f', 'mp4', '-movflags', 'frag_keyframe+delay_moov+default_base_moof', `${dir}/output.mp4`]);
        run(['-i', `${dir}/output.mp4`, '-c', 'copy', '-f', 'rtp', '-sdp_file', `${dir}/audio.sdp`, `${dir}/audio.rtp`]);
        assert.match(fs.readFileSync(`${dir}/audio.sdp`, 'utf8'), /config=1408/i);
    } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('bridge delivers short fragments even with three-second keyframe spacing', async () => {
    const { spawn } = require('node:child_process');
    const ts = require('typescript');
    const vm = require('node:vm');
    const context = { exports: {}, require, Buffer };
    vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/bridge.ts', 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true },
    }).outputText, context);
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cp4-fragments-'));
    try {
        const input = `${dir}/input.mp4`;
        const generated = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=15',
            '-f', 'lavfi', '-i', 'sine=sample_rate=16000', '-t', '5',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '45', '-keyint_min', '45',
            '-sc_threshold', '0', '-c:a', 'aac', input], { encoding: 'utf8', timeout: 15000 });
        assert.equal(generated.status, 0, generated.stderr);
        const args = Array.from(context.exports.remuxArguments(input));
        // Fixture container differs from camera PS; retain all production output options.
        args[args.indexOf('mpeg')] = 'mov';
        args.splice(args.indexOf('-i'), 0, '-re');
        const child = spawn('ffmpeg', args);
        let pending = Buffer.alloc(0), stderr = '';
        const fragments = [];
        child.stderr.on('data', data => { stderr += data; });
        child.stdout.on('data', data => {
            pending = Buffer.concat([pending, data]);
            while (pending.length >= 8) {
                const size = pending.readUInt32BE();
                if (size < 8 || pending.length < size) break;
                if (pending.toString('ascii', 4, 8) === 'moof') fragments.push(Date.now());
                pending = pending.subarray(size);
            }
        });
        const timer = setTimeout(() => child.kill('SIGKILL'), 15000);
        const code = await new Promise((resolve, reject) => {
            child.once('error', reject); child.once('close', resolve);
        }).finally(() => clearTimeout(timer));
        assert.equal(code, 0, stderr);
        assert.ok(fragments.length >= 15, `only ${fragments.length} fragments`);
        const gaps = fragments.slice(1).map((time, index) => time - fragments[index]);
        assert.ok(Math.max(...gaps) < 1000, `largest delivery gap ${Math.max(...gaps)}ms`);
    } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
