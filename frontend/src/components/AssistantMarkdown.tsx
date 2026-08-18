import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function AssistantMarkdown({
  text,
}: {
  text: string;
}) {
  return (
    <Markdown
      remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
      skipHtml
      components={{
        table({ node: _node, ...props }) {
          return (
            <div className="markdown-table-scroll">
              <table {...props} />
            </div>
          );
        },
      }}
    >
      {text}
    </Markdown>
  );
}
