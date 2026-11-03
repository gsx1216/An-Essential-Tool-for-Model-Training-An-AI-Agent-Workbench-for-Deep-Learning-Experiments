import MarkdownIt from "markdown-it";
export const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
});
const defaultLink = markdown.renderer.rules.link_open;
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noopener noreferrer");
  return defaultLink
    ? defaultLink(tokens, index, options, env, renderer)
    : renderer.renderToken(tokens, index, options);
};
// No images from generated text: avoids unsolicited third-party requests carrying private paths.
markdown.renderer.rules.image = (tokens, index) =>
  markdown.utils.escapeHtml(tokens[index].content || "图片");
export const renderMarkdown = (value: string) => markdown.render(value);
