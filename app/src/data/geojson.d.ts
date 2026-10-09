// .geojson files are bundled as JS modules by metro.transformer.js.
// Typed as unknown on purpose: src/data/courts.ts validates the shape at runtime.
declare module '*.geojson' {
  const data: unknown;
  export default data;
}
