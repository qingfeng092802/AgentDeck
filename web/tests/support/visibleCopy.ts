import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import * as ts from "typescript";

/** 只收集"会出现在产品界面上"的串：JSX 文本、可见属性、以及源码里的字符串字面量。
 *
 * 注释天然不在 AST 里；`className` / `style` / SVG 几何属性整支跳过 —— 那些串是实现面，
 * 把它们也扫进来会让"类名里恰好有本页"这种误报淹掉真问题。接口层 `lib/acp.ts` 不扫：
 * 它承载的是路径、HTTP 方法、字段名，`"POST"` 在那里是协议不是文案。
 */
const SKIP_FILES = new Set(["src/lib/acp.ts"]);
const VISIBLE_ATTRS = new Set([
  "title",
  "alt",
  "placeholder",
  "aria-label",
  "aria-description",
  "aria-placeholder",
]);

/** 颜色令牌不是文案。`#6B4226` 这种串和 `className` / `style` 同类，属于实现面 ——
 *  不跳的话，一个碰巧含 "422" 的色值会让"界面不许出现 HTTP 状态码"那条判据假红，
 *  而被逼着去改一个谁都看不出来的色值。 */
const COLOR_TOKEN = /^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

export interface VisibleCopyEntry {
  readonly file: string;
  readonly line: number;
  readonly text: string;
}

function walk(root: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === "dist") continue;
    const full = join(root, entry.name);
    if (entry.isDirectory()) {
      found.push(...walk(full));
      continue;
    }
    if (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx")) found.push(full);
  }
  return found;
}

function isInsideBannedTable(node: ts.Node, sourceFile: ts.SourceFile): boolean {
  for (let current: ts.Node | undefined = node; current; current = current.parent) {
    if (
      ts.isVariableDeclaration(current) &&
      ts.isIdentifier(current.name) &&
      current.name.text === "BANNED_TERMS"
    ) {
      return true;
    }
  }
  return false;
}

function isImportOrExportSpecifier(node: ts.Node): boolean {
  for (let current: ts.Node | undefined = node.parent; current; current = current.parent) {
    if (ts.isImportDeclaration(current) || ts.isExportDeclaration(current)) return true;
    if (ts.isSourceFile(current)) return false;
  }
  return false;
}

function isPropertyName(node: ts.Node): boolean {
  const parent = node.parent;
  return (
    (ts.isPropertyAssignment(parent) || ts.isPropertySignature(parent) || ts.isPropertyDeclaration(parent)) &&
    parent.name === node
  );
}

function isInsideNonVisibleJsxAttribute(node: ts.Node, sourceFile: ts.SourceFile): boolean {
  for (let current: ts.Node | undefined = node.parent; current; current = current.parent) {
    if (ts.isJsxAttribute(current)) {
      const name = current.name.getText(sourceFile);
      return !VISIBLE_ATTRS.has(name);
    }
    if (ts.isSourceFile(current)) return false;
  }
  return false;
}

/** 从一个源码文本里收集可见串。单独导出是为了让"扫描器自己"也有判据（见 copy.test.ts）。 */
export function collectFromSource(file: string, text: string): VisibleCopyEntry[] {
  if (SKIP_FILES.has(file)) return [];
  const sourceFile = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const found: VisibleCopyEntry[] = [];

  const push = (node: ts.Node, value: string): void => {
    const trimmed = value.replace(/\s+/g, " ").trim();
    if (!trimmed) return;
    if (COLOR_TOKEN.test(trimmed)) return;
    const at = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
    found.push({ file, line: at.line + 1, text: trimmed });
  };

  const visit = (node: ts.Node): void => {
    if (ts.isJsxText(node)) {
      push(node, node.text);
    } else if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      if (
        !isInsideBannedTable(node, sourceFile) &&
        !isImportOrExportSpecifier(node) &&
        !isPropertyName(node) &&
        !isInsideNonVisibleJsxAttribute(node, sourceFile)
      ) {
        push(node, node.text);
      }
    } else if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
      if (!isInsideBannedTable(node, sourceFile) && !isInsideNonVisibleJsxAttribute(node, sourceFile)) {
        push(node, node.text);
      }
    }
    ts.forEachChild(node, visit);
  };

  visit(sourceFile);
  return found;
}

export function collectVisibleCopy(root: string): VisibleCopyEntry[] {
  const found: VisibleCopyEntry[] = [];
  for (const full of walk(root)) {
    const rel = relative(join(root, ".."), full).replace(/\\/g, "/");
    found.push(...collectFromSource(rel, readFileSync(full, "utf8")));
  }
  return found;
}
