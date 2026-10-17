import { absoluteTime, formatRelative } from "@/lib/time";

export function RelativeTime({
  at,
  now,
  className,
}: {
  at: number;
  now: number;
  className?: string;
}) {
  const { dateTime, label } = absoluteTime(at);
  return (
    <time dateTime={dateTime} title={label} className={className}>
      {formatRelative(at, now)}
    </time>
  );
}
