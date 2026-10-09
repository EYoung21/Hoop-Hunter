// Wraps Expo's Babel transformer so .geojson files (which are JSON) become
// `module.exports = {...}`. Everything else goes through Expo's transformer unchanged.
const upstreamTransformer = require(
  require.resolve('@expo/metro-config/babel-transformer', {
    paths: [require.resolve('expo/package.json')],
  }),
);

module.exports = {
  ...upstreamTransformer,
  transform(params) {
    if (params.filename.endsWith('.geojson')) {
      // Parse first so a malformed file fails the build with a clear JSON error.
      const json = JSON.stringify(JSON.parse(params.src));
      return upstreamTransformer.transform({ ...params, src: `module.exports = ${json};` });
    }
    return upstreamTransformer.transform(params);
  },
};
