import { localeTag } from "./i18n";

function localDateTimeFormatter() {
  return new Intl.DateTimeFormat(localeTag(), {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZoneName: "short",
  });
}

export function formatLocalDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 16).replace("T", " ");
  return localDateTimeFormatter().format(date);
}
