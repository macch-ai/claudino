// wrangler bundles .md files as text (see "rules" in wrangler.jsonc).
declare module "*.md" {
  const text: string;
  export default text;
}
