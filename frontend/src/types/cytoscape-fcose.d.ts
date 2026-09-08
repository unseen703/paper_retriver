/**
 * `cytoscape-fcose` ships no type declarations and has no @types package.
 *
 * Declared as a Cytoscape extension registration function rather than `any`,
 * so `cytoscape.use(fcose)` is still checked for arity even though the
 * layout's own options are not. Its options are validated at the call site in
 * GraphCanvas.tsx by the `cytoscape.LayoutOptions` cast there.
 */
declare module "cytoscape-fcose" {
  import type cytoscape from "cytoscape";
  const fcose: cytoscape.Ext;
  export default fcose;
}
