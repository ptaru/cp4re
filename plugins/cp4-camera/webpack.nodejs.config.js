const webpack = require('webpack');
const config = require(process.env.SCRYPTED_DEFAULT_WEBPACK_CONFIG);

// SDK 0.5.59 detects SCRYPTED_SDK_CJS_MODULE but its webpack branch loads the
// legacy SCRYPTED_SDK_MODULE instead. Prefer the detected CJS runtime path.
config.plugins.push(new webpack.DefinePlugin({
    'process.env.SCRYPTED_SDK_MODULE': '(process.env.SCRYPTED_SDK_CJS_MODULE || process.env.SCRYPTED_SDK_MODULE)',
}));
config.resolve.alias['@scrypted/sdk$'] = require.resolve('@scrypted/sdk').replace('/dist/src/index.js', '/src/index.ts');
config.module.rules.unshift({
    test: /@scrypted[\\/]sdk[\\/]src[\\/]index\.ts$/,
    use: { loader: 'ts-loader', options: { compilerOptions: { module: 'esnext' }, transpileOnly: true } },
});
config.resolve.alias['../types/gen/index$'] = require.resolve('@scrypted/sdk').replace('/src/index.js', '/types/gen/index.js');
// The SDK source must use the ES module loader rule only.
config.module.rules[1].exclude = /@scrypted[\\/]sdk[\\/]src[\\/]index\.ts$/;
module.exports = config;
