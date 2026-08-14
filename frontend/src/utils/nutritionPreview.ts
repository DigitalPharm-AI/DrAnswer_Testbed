import type { ChatInput, ChatTable, ChatTableRow } from "../api/contracts";

const NUMERIC_VALUE_PATTERN = /^(-?\d+(?:\.\d+)?)\s*(.*)$/;
const NUTRIENT_SEGMENT_PATTERN = /^(.+?)\s+(-?\d+(?:\.\d+)?)\s*(.*)$/;

function formatPreviewNumber(value: number): string {
  return new Intl.NumberFormat("ko-KR", {
    maximumFractionDigits: 6,
  }).format(value);
}

function currentPortionValue(
  input: ChatInput,
  rawValue: string | undefined,
): string {
  const quantity = Number(rawValue);
  if (!rawValue || !Number.isFinite(quantity) || quantity <= 0) {
    return "섭취량 입력 필요";
  }
  return `${formatPreviewNumber(quantity)}${input.options.unit ?? ""}`;
}

function nutrientFactor(
  input: ChatInput,
  rawValue: string | undefined,
): number | null {
  const referenceQuantity = Number(input.value);
  const currentQuantity = Number(rawValue);
  if (
    !rawValue ||
    !Number.isFinite(referenceQuantity) ||
    referenceQuantity <= 0 ||
    !Number.isFinite(currentQuantity) ||
    currentQuantity <= 0
  ) {
    return null;
  }
  return currentQuantity / referenceQuantity;
}

function scaleValue(value: string, factor: number | null): string {
  const match = NUMERIC_VALUE_PATTERN.exec(value.trim());
  if (!match) {
    return value;
  }
  if (factor === null) {
    return "섭취량 입력 필요";
  }
  return `${formatPreviewNumber(Number(match[1]) * factor)}${
    match[2] ? ` ${match[2]}` : ""
  }`;
}

function scaleNutrientSummary(
  value: string,
  factor: number | null,
): string {
  return value
    .split(" · ")
    .map((segment) => {
      const match = NUTRIENT_SEGMENT_PATTERN.exec(segment.trim());
      if (!match) {
        return segment;
      }
      if (factor === null) {
        return `${match[1]} 섭취량 입력 필요`;
      }
      return `${match[1]} ${formatPreviewNumber(
        Number(match[2]) * factor,
      )}${match[3] ? ` ${match[3]}` : ""}`;
    })
    .join(" · ");
}

function previewRows(
  table: ChatTable,
  input: ChatInput,
  rawValue: string | undefined,
): ChatTableRow[] {
  const factor = nutrientFactor(input, rawValue);
  return table.rows.flatMap((row) => {
    if (row.column === "기준 제공량") {
      return [
        row,
        {
          column: "현재 섭취량",
          value: currentPortionValue(input, rawValue),
        },
      ];
    }
    if (row.column === "열량") {
      return [{ ...row, value: scaleValue(row.value, factor) }];
    }
    if (row.column === "영양성분") {
      return [
        {
          ...row,
          value: scaleNutrientSummary(row.value, factor),
        },
      ];
    }
    return [row];
  });
}

export function nutritionPreviewTables(
  tables: ChatTable[],
  inputs: ChatInput[],
  values: Record<string, string>,
): ChatTable[] {
  return tables.map((table, index) => {
    const input = inputs[index];
    if (!input || input.type !== "number") {
      return table;
    }
    return {
      ...table,
      rows: previewRows(table, input, values[input.label]),
    };
  });
}
