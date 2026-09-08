/**
 * `cytoscape-cola` ships no type declarations and has no @types package.
 *
 * Declared as a Cytoscape extension registration function rather than `any`,
 * so `cytoscape.use(cola)` is still arity-checked. Its layout options are
 * validated at the call site in GraphCanvas.tsx by the
 * `cytoscape.LayoutOptions` cast there, the same way fcose's are.
 */
declare module "cytoscape-cola" {
  import type cytoscape from "cytoscape";
  const cola: cytoscape.Ext;
  export default cola;
}
