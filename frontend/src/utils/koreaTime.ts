const KOREA_TIME_ZONE = "Asia/Seoul";

const koreaDateParts = new Intl.DateTimeFormat("en-US", {
  timeZone: KOREA_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function dateTimeParts(value: string | Date): Record<string, string> {
  return Object.fromEntries(
    koreaDateParts
      .formatToParts(value instanceof Date ? value : new Date(value))
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
}

export function koreaDateKey(value: string | Date): string {
  const parts = dateTimeParts(value);
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function formatKoreaClock(value: string): string {
  const parts = dateTimeParts(value);
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

export function formatKoreaTime(value: string): string {
  const parts = dateTimeParts(value);
  return `${parts.hour}:${parts.minute}`;
}

export function formatKoreanCalendarDate(date: string): string {
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: KOREA_TIME_ZONE,
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  }).format(new Date(`${date}T00:00:00+09:00`));
}

export function formatKoreanMessageTime(value: string): string {
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: KOREA_TIME_ZONE,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(value));
}
