import js from "@eslint/js";
import ts from "typescript-eslint";
import vue from "eslint-plugin-vue";
export default ts.config(
  { ignores: ["dist/**", "node_modules/**", "src/types/api-schema.d.ts"] },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...vue.configs["flat/recommended"],
  {
    files: ["**/*.vue"],
    languageOptions: { parserOptions: { parser: ts.parser } },
  },
  {
    languageOptions: {
      globals: {
        window: "readonly",
        document: "readonly",
        navigator: "readonly",
        sessionStorage: "readonly",
        localStorage: "readonly",
        fetch: "readonly",
        AbortController: "readonly",
        AbortSignal: "readonly",
        Blob: "readonly",
        URL: "readonly",
        File: "readonly",
        FormData: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        ResizeObserver: "readonly",
        HTMLElement: "readonly",
        DOMException: "readonly",
        crypto: "readonly",
        CustomEvent: "readonly",
        Event: "readonly",
        KeyboardEvent: "readonly",
        console: "readonly",
        process: "readonly",
        Buffer: "readonly",
        HTMLInputElement: "readonly",
        HTMLTextAreaElement: "readonly",
      },
    },
    rules: {
      "vue/multi-word-component-names": "off",
      "vue/max-attributes-per-line": "off",
      "vue/html-self-closing": "off",
      "vue/html-closing-bracket-newline": "off",
      "vue/singleline-html-element-content-newline": "off",
      "vue/multiline-html-element-content-newline": "off",
      "vue/first-attribute-linebreak": "off",
      "vue/html-indent": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  // Test cases intentionally mount independent components in the same suite.
  { files: ['tests/**/*.ts'], rules: { 'vue/one-component-per-file': 'off' } },
);
