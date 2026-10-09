// Learn more: https://docs.expo.dev/guides/customizing-metro/
const { getDefaultConfig } = require('expo/metro-config');

const config = getDefaultConfig(__dirname);

// Let `import courts from './courts.geojson'` resolve. The transformer below
// turns the file into a plain JS module, so the data is bundled with the app.
config.resolver.sourceExts.push('geojson');
config.transformer.babelTransformerPath = require.resolve('./metro.transformer.js');

module.exports = config;
