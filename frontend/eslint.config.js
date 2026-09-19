/**
 * ESLint flat config.
 *
 * **There was no config at all until code review looked.** `package.json` had
 * carried `"lint": "eslint ."` since R1, and ESLint 9+ requires an
 * `eslint.config.*`, so the script had never once run — meaning
 * `react-hooks` and `jsx-a11y` had never examined a line of this codebase.
 *
 * That was not academic. The two rule sets below are exactly the lanes that
 * would have caught the bulk-import unmount bug (an async loop over state with
 * no cleanup) and the clipped file input's interaction with the dialog's focus
 * trap, both of which reached review instead.
 *
 * ESLint is pinned to 9 rather than 10: `eslint-plugin-jsx-a11y@6.10.2` peers
 * on `^3 || … || ^9`, so installing 10 resolves to a broken tree.
 */
import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";

export default tseslint.config(
  {
    // Generated from OpenAPI by `npm run types`; CLAUDE.md forbids hand-editing
    // it, so linting it would only ever produce findings nobody may act on.
    ignores: ["dist/**", "node_modules/**", "src/types/api.ts"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser, ...globals.es2021 },
    },
    plugins: { "react-hooks": reactHooks, "jsx-a11y": jsxA11y },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,

      // An unused argument is often a signature being honoured deliberately —
      // a fetch stub that branches on `init` and ignores `url`. Underscore is
      // the conventional way to say so out loud.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    // Tests reach for `any` when stubbing wire shapes, and a `[]` cast through
    // `unknown` is the honest way to type a `vi.fn()` call record. Neither is
    // worth failing a build over in test code.
    files: ["**/*.test.{ts,tsx}"],
    rules: { "@typescript-eslint/no-explicit-any": "off" },
  },
);
