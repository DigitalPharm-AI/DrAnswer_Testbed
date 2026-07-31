import type { ReactNode } from "react";

type MarkdownBlock =
  | { kind: "paragraph"; text: string }
  | { kind: "unordered-list"; items: string[] }
  | { kind: "ordered-list"; items: string[] };

function inlineStrong(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /\*\*([^*]+?)\*\*/g;
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index));
    }
    nodes.push(
      <strong key={`strong-${match.index}`}>{match[1]}</strong>,
    );
    cursor = match.index + match[0].length;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return nodes;
}

function parseBlocks(source: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const paragraphLines: string[] = [];
  let listKind: "unordered-list" | "ordered-list" | null = null;
  let listItems: string[] = [];

  function flushParagraph() {
    const text = paragraphLines.join(" ").trim();
    if (text) {
      blocks.push({ kind: "paragraph", text });
    }
    paragraphLines.length = 0;
  }

  function flushList() {
    if (listKind && listItems.length) {
      blocks.push({ kind: listKind, items: listItems });
    }
    listKind = null;
    listItems = [];
  }

  for (const rawLine of source.replace(/\r\n?/g, "\n").split("\n")) {
    const line = rawLine.trim();
    if (!line) {
      flushParagraph();
      continue;
    }

    const unordered = line.match(/^[-+*]\s+(.+)$/);
    const ordered = line.match(/^\d+\.\s+(.+)$/);
    const nextListKind = unordered
      ? "unordered-list"
      : ordered
        ? "ordered-list"
        : null;

    if (nextListKind) {
      flushParagraph();
      if (listKind && listKind !== nextListKind) {
        flushList();
      }
      listKind = nextListKind;
      listItems.push((unordered?.[1] ?? ordered?.[1] ?? "").trim());
      continue;
    }

    flushList();
    paragraphLines.push(line);
  }

  flushParagraph();
  flushList();
  return blocks;
}

export default function AssistantMarkdown({
  text,
}: {
  text: string;
}) {
  const blocks = parseBlocks(text);
  return (
    <>
      {blocks.map((block, blockIndex) => {
        if (block.kind === "paragraph") {
          return (
            <p key={`paragraph-${blockIndex}`}>
              {inlineStrong(block.text)}
            </p>
          );
        }

        const List = block.kind === "ordered-list" ? "ol" : "ul";
        return (
          <List key={`list-${blockIndex}`}>
            {block.items.map((item, itemIndex) => (
              <li key={`item-${blockIndex}-${itemIndex}`}>
                {inlineStrong(item)}
              </li>
            ))}
          </List>
        );
      })}
    </>
  );
}
