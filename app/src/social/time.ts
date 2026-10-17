/**
 * Times for check-ins and runs. The server works in UTC plus the phone's offset; the app shows and picks
 * times in the phone's own time zone.
 */

/**
 * Minutes to add to UTC for local time (Chattanooga in summer: -240), as the RPCs expect. Real offsets
 * run from -12:00 to +14:00 in 15-minute steps.
 */
export function utcOffset(date = new Date()): number {
  const minutes = Math.round(-date.getTimezoneOffset() / 15) * 15;
  return Math.max(-720, Math.min(840, minutes));
}

/** Pickable start times run from 5:00 AM to 11:30 PM in half-hour steps. */
const FIRST_SLOT_MINUTES = 5 * 60;
const LAST_SLOT_MINUTES = 23 * 60 + 30;
const SLOT_STEP = 30;
/** A slot must start at least this far ahead to be offered for today. */
const SLOT_LEAD_MS = 5 * 60_000;

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

/** Local midnight `offset` days from today. */
export function dayStart(offset: number, now = new Date()): Date {
  const day = startOfDay(now);
  day.setDate(day.getDate() + offset);
  return day;
}

/** Start times on a day (0 = today), skipping ones already past. */
export function slotsForDay(offset: number, now = new Date()): Date[] {
  const base = dayStart(offset, now);
  const slots: Date[] = [];
  for (let m = FIRST_SLOT_MINUTES; m <= LAST_SLOT_MINUTES; m += SLOT_STEP) {
    const slot = new Date(base.getFullYear(), base.getMonth(), base.getDate(), Math.floor(m / 60), m % 60);
    if (slot.getTime() > now.getTime() + SLOT_LEAD_MS) slots.push(slot);
  }
  return slots;
}

export function dayLabel(offset: number, now = new Date()): string {
  if (offset === 0) return 'Today';
  if (offset === 1) return 'Tomorrow';
  return dayStart(offset, now).toLocaleDateString(undefined, { weekday: 'short' });
}

export function formatTime(value: Date | string): string {
  const date = typeof value === 'string' ? new Date(value) : value;
  return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

/** "6:30 PM", "Tomorrow 6:30 PM" or "Sat 6:30 PM". */
export function formatWhen(value: Date | string, now = new Date()): string {
  const date = typeof value === 'string' ? new Date(value) : value;
  const days = Math.round((startOfDay(date).getTime() - startOfDay(now).getTime()) / 86_400_000);
  if (days === 0) return formatTime(date);
  if (days === 1) return `Tomorrow ${formatTime(date)}`;
  if (days === -1) return `Yesterday ${formatTime(date)}`;
  return `${date.toLocaleDateString(undefined, { weekday: 'short' })} ${formatTime(date)}`;
}

/** A random v4 UUID (ids for runs made on the phone). */
export function newId(): string {
  const hex = '0123456789abcdef';
  let out = '';
  for (let i = 0; i < 36; i += 1) {
    if (i === 8 || i === 13 || i === 18 || i === 23) out += '-';
    else if (i === 14) out += '4';
    else if (i === 19) out += hex[8 + Math.floor(Math.random() * 4)];
    else out += hex[Math.floor(Math.random() * 16)];
  }
  return out;
}
