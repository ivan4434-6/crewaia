import { describe, expect, it } from "vitest";
import { formatBytes, formatClock, formatDuration, formatPrecise, fromLocalInput, knownTimeZones, parseClock, plural, readFigure, timeAgo, timeSince, toLocalInput } from "./format";

describe("format helpers", () => {
  it("formats clocks and durations", () => {
    expect(formatClock(83.4)).toBe("1:23");
    expect(formatClock(3725)).toBe("1:02:05");
    expect(formatClock(null)).toBe("—");
    expect(formatDuration(0.4)).toBe("menos de 1 s");
    expect(formatDuration(95)).toBe("1 min 35 s");
    expect(formatDuration(3700)).toBe("1 h 1 min");
  });

  it("parses what people type as a timestamp", () => {
    expect(parseClock("1:23")).toBe(83);
    expect(parseClock("83,5")).toBe(83.5);
    expect(parseClock("1:02:05")).toBe(3725);
    expect(parseClock("abc")).toBeNull();
    expect(parseClock("")).toBeNull();
  });

  it("reads typed figures without guessing between thousands and decimals", () => {
    const value = (text: string, whole: boolean) => {
      const figure = readFigure(text, whole);
      return figure.ok ? figure.value : figure.reason;
    };
    // Counts: a grouped number can only be thousands, and decimals are refused.
    expect(value("1840", true)).toBe(1840);
    expect(value("1.840", true)).toBe(1840);
    expect(value("1,840", true)).toBe(1840);
    expect(value("12 345", true)).toBe(12345);
    expect(value("1.234.567", true)).toBe(1234567);
    expect(value("61,5", true)).toBe("whole");
    // Figures with decimals: comma or point, but "1.840" could be either reading.
    expect(value(" 61,5 ", false)).toBe(61.5);
    expect(value("61.5", false)).toBe(61.5);
    expect(value("1840,25", false)).toBe(1840.25);
    expect(value("1.840", false)).toBe("ambiguous");
    expect(value("1.234.567", false)).toBe(1234567);
    for (const text of ["", "abc", "-5", "12 %", "1e3", "1,2,3", ",", "1.840,5"]) expect(value(text, false)).toBe("format");
  });

  it("always offers UTC among the time zones", () => {
    expect(knownTimeZones()).toContain("UTC");
    expect(knownTimeZones()).toContain("Europe/Madrid");
  });

  it("keeps trim points stable when shown and read back", () => {
    expect(formatPrecise(83.46)).toBe("1:23.5");
    expect(formatPrecise(5)).toBe("0:05.0");
    expect(formatPrecise(4503.2)).toBe("75:03.2");
    for (const seconds of [0, 5.3, 59.9, 60, 83.5, 4503.2]) {
      expect(parseClock(formatPrecise(seconds))).toBeCloseTo(seconds, 5);
    }
    expect(formatPrecise(59.96)).toBe("1:00.0");
  });

  it("pluralises and sizes", () => {
    expect(plural(1, "clip")).toBe("1 clip");
    expect(plural(3, "clip")).toBe("3 clips");
    expect(plural(2, "sección", "secciones")).toBe("2 secciones");
    expect(formatBytes(1536)).toBe("2 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5,0 MB");
  });

  it("describes relative time and round-trips local inputs", () => {
    const now = Date.parse("2026-11-11T12:00:00Z");
    expect(timeAgo("2026-11-11T11:59:50Z", now)).toBe("ahora mismo");
    expect(timeAgo("2026-11-11T11:30:00Z", now)).toBe("hace 30 minutos");
    expect(timeAgo("2026-11-12T12:00:00Z", now)).toBe("mañana");
    // An event that arrives while the page clock is stale, or from a server a little ahead, is not "in the future".
    expect(timeAgo("2026-11-11T12:00:20Z", now)).toBe("en unos segundos");
    expect(timeSince("2026-11-11T12:00:20Z", now)).toBe("ahora mismo");
    expect(timeSince("2026-11-11T12:05:00Z", now)).toBe("ahora mismo");
    expect(timeSince("2026-11-11T11:30:00Z", now)).toBe("hace 30 minutos");
    const iso = "2026-11-11T12:30:00.000Z";
    expect(fromLocalInput(toLocalInput(iso))).toBe(iso);
    expect(fromLocalInput("")).toBeNull();
  });
});
