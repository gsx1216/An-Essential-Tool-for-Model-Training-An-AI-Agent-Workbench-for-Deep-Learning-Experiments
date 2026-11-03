import ts from "typescript";
import { readFileSync } from "node:fs";
const source = ts.createSourceFile(
  "api.ts",
  readFileSync(new URL("../src/api/index.ts", import.meta.url), "utf8"),
  ts.ScriptTarget.Latest,
  true,
);
const spec = JSON.parse(
  readFileSync(new URL("../docs/api/openapi.json", import.meta.url), "utf8"),
);
const normalize = (path) => path.replace(/\{[^}]*\}/g, "{}");
const known = new Set(
  Object.entries(spec.paths).flatMap(([path, methods]) =>
    Object.keys(methods)
      .filter((m) => ["get", "post", "put", "delete"].includes(m))
      .map((method) => `${method} ${normalize(path)}`),
  ),
);
let checked = 0;
const missing = [];
function walk(node) {
  if (ts.isCallExpression(node)) {
    const name = node.expression.getText(source).replace(/^http\./, "");
    const method =
      name === "del" ? "delete" : name === "streamPost" ? "post" : name;
    const first = node.arguments[0];
    if (["get", "post", "put", "delete"].includes(method) && first) {
      let path;
      if (ts.isStringLiteral(first)) path = first.text;
      else if (ts.isTemplateExpression(first))
        path =
          first.head.text +
          first.templateSpans.map((s) => "{}" + s.literal.text).join("");
      if (path) {
        const candidates =
          path.includes("{}-settings") || path.includes("{}-tasks/")
            ? ["analysis", "suggestion"].map((name) =>
                path
                  .replace("{}-settings", name + "-settings")
                  .replace("{}-tasks/", name + "-tasks/"),
              )
            : [path];
        for (const candidate of candidates) {
          checked++;
          if (!known.has(`${method} ${candidate}`))
            missing.push(`${method.toUpperCase()} ${candidate}`);
        }
      }
    }
  }
  ts.forEachChild(node, walk);
}
walk(source);
if (missing.length) {
  console.error("接口与后端 OpenAPI 不一致：\n" + missing.join("\n"));
  process.exitCode = 1;
} else console.log(`PASS: ${checked} 处 API 调用路径/方法与后端快照一致。`);
